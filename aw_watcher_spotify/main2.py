#!/usr/bin/env python3

import sys
import logging
import time
import traceback
from typing import Optional
from time import sleep
from datetime import datetime, timezone, timedelta
import json

from requests import ConnectionError
import spotipy
# import spotipy.util as util
# from spotipy.oauth2 import SpotifyClientCredentials
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
from pathlib import Path

from aw_core import dirs
from aw_core.models import Event
from aw_client.client import ActivityWatchClient

logger = logging.getLogger("aw-watcher-spotify")
DEFAULT_CONFIG = """
[aw-watcher-spotify]
username = "tonkatommy"
client_id = "1e1ac4ee762a462e8f034d8683f38222"
client_secret = "d9a0a51d8f454778aecd5137888523d5"
poll_time = 5.0"""

# ------------------ Setup logging config ------------------
logging.basicConfig(
  level=logging.INFO,
  format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
  datefmt="%Y-%m-%d %H:%M:%S",
)

# ------------------ Get current track ------------------
def get_current_track(sp) -> Optional[dict]:
    current_track = sp.currently_playing(additional_types=["episode"])
    if current_track and current_track["is_playing"]:
        return current_track
    return None

failed_audio_features = set()

# ------------------ Get data from track ------------------
def data_from_track(track: dict, sp, failed_ids=None) -> dict:
    if not track or not track.get("item"):
        return {}

    song_name = track["item"]["name"]
    track_id = track["item"]["id"]

    data = {
        "title": song_name,
        "uri": track["item"]["uri"]
    }

    if track["item"]["type"] == "track":
        data["artist"] = track["item"]["artists"][0]["name"]
        data["album"] = track["item"]["album"]["name"]
        data["popularity"] = track["item"].get("popularity", -1)

        if track_id and (failed_ids is None or track_id not in failed_ids):
            try:
                features = sp.audio_features(track_id)[0]
                if features:
                    data.update(features)
            except spotipy.SpotifyException as e:
                logger.warning(f"Could not fetch audio features for track (likely unavailable): {e}")
                if failed_ids is not None:
                    failed_ids.add(track_id)
            except Exception as e:
                logger.warning(f"Unexpected error fetching audio features: {e}")
                if failed_ids is not None:
                    failed_ids.add(track_id)

        logging.debug("TRACK: {} - {} ({})".format(data["title"], data["artist"], data["album"]))

    elif track["item"]["type"] == "episode":
        data["artist"] = track["item"]["show"]["publisher"]
        data["album"] = track["item"]["show"]["name"]
        logging.debug("EPISODE: {} - {}".format(data["title"], data["artist"]))

    return data

# ------------------ Auth ------------------
def auth(client_id=None, client_secret=None):
    
    try:
        token_path = str(Path(__file__).resolve().parent / "spotify_token.json")

        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri="http://127.0.0.1:8088",
            # redirect_uri="http://localhost:8888/callback",
            scope="user-read-playback-state user-read-currently-playing",
            cache_path=token_path
        )

        logger.info(f"Using token cache file: {token_path}")
        token_info = auth_manager.get_cached_token()
        if token_info:
            expires_in = token_info['expires_at'] - int(time.time())
            logger.info(f"Token expires in {expires_in} seconds")
        else:
            logger.info("No cached token found, will authenticate interactively.")

        sp = spotipy.Spotify(auth_manager=auth_manager)

        # Print the user's display name
        print("Logged in as:", sp.me()["display_name"])
        logger.info("Successfully authenticated with Spotify.")


        # Test the token immediately by calling a lightweight endpoint
        sp.current_user()
        return sp

    except SpotifyOauthError as e:
        logger.error("Spotify OAuth error during authentication.")
        logger.error(f"Details: {e}")
        sys.exit(1)

    except Exception as e:
        logger.error("Unexpected error during Spotify authentication.")
        logger.error(traceback.format_exc())
        sys.exit(1)

# ------------------ Load config ------------------
def load_config():
    from aw_core.config import load_config_toml as _load_config

    return _load_config("aw-watcher-spotify", DEFAULT_CONFIG)

# ------------------ Print status line ------------------
def print_statusline(msg):
    last_msg_length = (
        len(print_statusline.last_msg) if hasattr(print_statusline, "last_msg") else 0
    )
    print(" " * last_msg_length, end="\r")  # Clear old message
    print(msg + " " * max(0, last_msg_length - len(msg)), end="\r")
    print_statusline.last_msg = msg

# ------------------ Main Loop ------------------
def main():
    

    config_dir = dirs.get_config_dir("aw-watcher-spotify")

    config = load_config()
    poll_time = float(config["aw-watcher-spotify"].get("poll_time"))
    username = config["aw-watcher-spotify"].get("username", None)
    client_id = config["aw-watcher-spotify"].get("client_id", None)
    client_secret = config["aw-watcher-spotify"].get("client_secret", None)
    if not username or not client_id or not client_secret:
        logger.warning(
            "username, client_id or client_secret not specified in config file (in folder {}). Get your client_id and client_secret here: https://developer.spotify.com/my-applications/".format(
                config_dir
            )
        )
        sys.exit(1)

    # Console log username, client_id and client_secret and poll_time
    logger.info("Username: {}".format(username))
    logger.info("Client ID: {}".format(client_id))
    logger.info("Client Secret: {}".format(client_secret))
    logger.info("Poll Time: {}".format(poll_time))

    # TODO: Fix --testing flag and set testing as appropriate
    aw = ActivityWatchClient("aw-watcher-spotify", testing=False)
    bucketname = "{}_{}".format(aw.client_name, aw.client_hostname)
    aw.create_bucket(bucketname, "currently-playing", queued=True)
    aw.connect()

    sp = auth(client_id=client_id, client_secret=client_secret)
    last_track = None
    track = None
    while True:
        try:
            track = get_current_track(sp)
            # from pprint import pprint
            # pprint(track)
        except spotipy.client.SpotifyException as e:
            print_statusline("\nToken expired, trying to refresh\n")
            sp = auth(client_id=client_id, client_secret=client_secret)
            continue
        except ConnectionError as e:
            logger.error(
                "Connection error while trying to get track, check your internet connection."
            )
            sleep(poll_time)
            continue
        except json.JSONDecodeError as e:
            logger.error("Error trying to decode")
            sleep(0.1)
            continue
        except Exception as e:
            logger.error("Unknown Error")
            logger.error(traceback.format_exc())
            sleep(0.1)
            continue

        try:
            # Outputs a new line when a song ends, giving a short history directly in the log
            if last_track:
                last_track_data = data_from_track(last_track, sp, failed_audio_features)
                if not track or (
                    track
                    and last_track_data["uri"] != data_from_track(track, sp, failed_audio_features)["uri"]
                ):
                    song_td = timedelta(seconds=last_track["progress_ms"] / 1000)
                    song_time = int(song_td.seconds / 60), int(song_td.seconds % 60)
                    print_statusline(
                        "Track ended ({}:{:02d}): {title} - {artist} ({album})\n".format(
                            *song_time, **last_track_data
                        )
                    )

            if track:
                track_data = data_from_track(track, sp, failed_audio_features)
                song_td = timedelta(seconds=track["progress_ms"] / 1000)
                song_time = int(song_td.seconds / 60), int(song_td.seconds % 60)

                print_statusline(
                    "Current track ({}:{:02d}): {title} - {artist} ({album})".format(
                        *song_time, **track_data
                    )
                )

                event = Event(timestamp=datetime.now(timezone.utc), data=track_data)
                aw.heartbeat(bucketname, event, pulsetime=poll_time + 1, queued=True)
            else:
                print_statusline("Waiting for track to start playing...")

            last_track = track
        except Exception as e:
            print("An exception occurred: {}".format(e))
            traceback.print_exc()
        sleep(poll_time)


if __name__ == "__main__":
    logging.info("Listening with aw-watcher-spotify...")
    main()
