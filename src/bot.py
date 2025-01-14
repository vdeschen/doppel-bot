import os
import time
import re

from fastapi import Request, FastAPI

from modal import Dict, Secret, asgi_app

from .common import (
    MULTI_WORKSPACE_SLACK_APP,
    VOL_MOUNT_PATH,
    output_vol,
    get_user_for_team_id,
    stub,
)
from .inference import OpenLlamaModel
from .scrape import scrape
from .finetune import finetune

# Ephemeral caches
stub.users_cache = Dict.new()
stub.self_cache = Dict.new()

MAX_INPUT_LENGTH = 512  # characters, not tokens.


def get_users(team_id: str, client) -> dict[str, tuple[str, str]]:
    """Returns a mapping from display name to user ID and avatar."""
    try:
        users = stub.app.users_cache[team_id]
    except KeyError:
        # TODO: lower TTL when we support it.
        users = {}
        cursor = None
        while True:
            result = client.users_list(limit=1000, cursor=cursor)
            for user in result["members"]:
                users[user["profile"]["display_name"]] = (user["id"], user["profile"]["image_512"])
                users[user["profile"]["real_name"]] = (user["id"], user["profile"]["image_512"])

            if not result["has_more"]:
                break

            cursor = result["response_metadata"]["next_cursor"]
        stub.app.users_cache[team_id] = users
    return users


def get_self_id(team_id: str, client) -> str:
    try:
        # TODO: lower TTL when we support it.
        return stub.app.self_cache[team_id]
    except KeyError:
        stub.app.self_cache[team_id] = self_id = client.auth_test(team_id=team_id)["user_id"]
        return self_id


def get_oauth_settings():
    from slack_bolt.oauth.oauth_settings import OAuthSettings
    from slack_sdk.oauth.installation_store import FileInstallationStore
    from slack_sdk.oauth.state_store import FileOAuthStateStore

    return OAuthSettings(
        client_id=os.environ["SLACK_CLIENT_ID"],
        client_secret=os.environ["SLACK_CLIENT_SECRET"],
        scopes=[
            "app_mentions:read",
            "channels:history",
            "channels:join",
            "channels:read",
            "chat:write",
            "chat:write.customize",
            "commands",
            "users.profile:read",
            "users:read",
        ],
        install_page_rendering_enabled=False,
        installation_store=FileInstallationStore(base_dir=VOL_MOUNT_PATH / "slack" / "installation"),
        state_store=FileOAuthStateStore(expiration_seconds=600, base_dir=VOL_MOUNT_PATH / "slack" / "state"),
    )


@stub.function(
    image=stub.slack_image,
    secrets=[
        Secret.from_name("slack-finetune-secret"),
        # TODO: Modal should support optional secrets.
        *([Secret.from_name("neon-secret")] if MULTI_WORKSPACE_SLACK_APP else []),
    ],
    # Has to outlive both scrape and finetune.
    timeout=60 * 60 * 4,
    network_file_systems={VOL_MOUNT_PATH: output_vol},
    cloud="gcp",
    keep_warm=1,
)
@asgi_app(label="doppel")
def _asgi_app():
    from slack_bolt import App
    from slack_bolt.adapter.fastapi import SlackRequestHandler

    if MULTI_WORKSPACE_SLACK_APP:
        slack_app = App(oauth_settings=get_oauth_settings())
    else:
        slack_app = App(
            signing_secret=os.environ["SLACK_SIGNING_SECRET"],
            token=os.environ["SLACK_BOT_TOKEN"],
        )

    fastapi_app = FastAPI()
    handler = SlackRequestHandler(slack_app)

    @slack_app.event("url_verification")
    def handle_url_verification(body, logger):
        challenge = body.get("challenge")
        return {"challenge": challenge}

    @slack_app.event("app_mention")
    def handle_app_mentions(body, say, client):
        team_id = body["team_id"]
        channel_id = body["event"]["channel"]
        ts = body["event"].get("thread_ts", body["event"]["ts"])

        users = get_users(team_id, client)
        self_id = get_self_id(team_id, client)

        messages = client.conversations_replies(channel=channel_id, ts=ts, limit=1000)["messages"]
        messages.sort(key=lambda m: m["ts"])

        # Go backwards and fetch messages until we hit the max input length.
        inputs = []
        total = 0
        for message in reversed(messages):
            if "user" in message:
                i = f"{message['user']}: {message['text']}"
            else:
                i = message["text"]
            i = i.replace(f"<@{self_id}>", "")
            inputs.append(i)
            total += len(i)

            if total > MAX_INPUT_LENGTH:
                break

        input = "\n".join(reversed(inputs))

        print("Input: ", input)

        user = get_user_for_team_id(team_id, users.keys())
        if user is None:
            say(text="No users trained yet. Run /doppel <user> first.", thread_ts=ts)
            return
        _, avatar_url = users[user]

        model = OpenLlamaModel.remote(user, team_id)
        res = model.generate(
            input,
            do_sample=True,
            temperature=0.3,
            top_p=0.85,
            top_k=40,
            num_beams=1,
            max_new_tokens=600,
            repetition_penalty=1.2,
        )

        exp = "|".join([f"{u}: " for u, _ in users.values()])
        messages = re.split(exp, res)

        print("Generated: ", res, messages)

        for message in messages:
            if message:
                client.chat_postMessage(
                    channel=channel_id,
                    text=message,
                    thread_ts=ts,
                    icon_url=avatar_url,
                    username=f"{user}-bot",
                )

    @slack_app.command("/doppel")
    def handle_doppel(ack, respond, command, client):
        ack()
        team_id = command["team_id"]
        users = get_users(team_id, client)

        user = command["text"]
        if user not in users:
            return respond(text=f"User {user} not found.")

        user_pipeline.spawn(team_id, client.token, user, respond)

    @fastapi_app.post("/")
    async def root(request: Request):
        return await handler.handle(request)

    @fastapi_app.get("/slack/install")
    async def oauth_start(request: Request):
        return await handler.handle(request)

    @fastapi_app.get("/slack/oauth_redirect")
    async def oauth_callback(request: Request):
        return await handler.handle(request)

    return fastapi_app


@stub.function(
    image=stub.slack_image,
    # TODO: Modal should support optional secrets.
    secret=Secret.from_name("neon-secret") if MULTI_WORKSPACE_SLACK_APP else None,
    # Has to outlive both scrape and finetune.
    timeout=60 * 60 * 4,
)
def user_pipeline(team_id: str, token: str, user: str, respond):
    from .db import insert_user, update_state, delete_user

    try:
        if MULTI_WORKSPACE_SLACK_APP:
            state, handle = insert_user(team_id, user)
            if handle is not None:
                return respond(text=f"Team {team_id} already has {handle} registered (state={state}).")
        respond(text=f"Began scraping {user}.")
        samples = scrape.call(user, team_id, bot_token=token)
        respond(text=f"Finished scraping {user} (found {samples} samples), starting training.")

        if MULTI_WORKSPACE_SLACK_APP:
            update_state(team_id, user, "training")

        t0 = time.time()

        finetune.call(user, team_id)

        respond(text=f"Finished training {user} after {time.time() - t0:.2f} seconds.")

        if MULTI_WORKSPACE_SLACK_APP:
            update_state(team_id, user, "success")
    except Exception as e:
        respond(text=f"Failed to train {user} ({e}). Try again in a bit!")
        if MULTI_WORKSPACE_SLACK_APP:
            delete_user(team_id, user)
        raise e
