import os
import re
import subprocess
import time
import traceback

import cassiopeia as cass
import datapipelines
import requests
from cassiopeia import get_current_match

from lib.builders import description_builder, tags_builder
from lib.externals_sites import opgg_extractor
from lib.managers import replay_api_manager, obs_manager, league_manager, upload_manager

from dotenv import load_dotenv

from lib.builders.title_builder import get_title
from lib.managers.game_cfg_manager import enable_settings, disable_settings
from lib.managers.league_manager import bugsplat_exists, kill_bugsplat
from lib.managers.replay_api_manager import get_player_position, PortNotFoundException
from lib.utils import pretty_log

load_dotenv()

WAIT_TIME = 2
SURREND_TIME = 15 * 60
BAT_PATH = 'replay.bat'
VIDEOS_PATH = 'D:\\LeagueReplays\\'


def wait_finish():
    current_time = replay_api_manager.get_current_game_time()
    while True:
        if bugsplat_exists():
            raise GameCrashedException
        new_time = replay_api_manager.get_current_game_time()
        # paused = replay_api_manager.is_game_paused()
        # if not paused and new_time == current_time and new_time >= SURREND_TIME:
        if new_time == current_time:
            print("[SPECTATOR] - Game ended")
            return
        current_time = new_time
        time.sleep(0.5)


class GameCrashedException(Exception):
    pass


def wait_for_game_launched():
    while True:
        try:
            if replay_api_manager.game_launched():
                print("[SPECTATOR] - Game has launched")
                break
            if bugsplat_exists():
                raise GameCrashedException
        except (requests.exceptions.ConnectionError, subprocess.CalledProcessError):
            print("[SPECTATOR] - Game not yet launched")
            time.sleep(1)


def wait_for_game_start():
    while True:
        current_time = replay_api_manager.get_current_game_time()
        if current_time <= 5:
            wait_seconds(WAIT_TIME)
            continue
        if bugsplat_exists():
            raise GameCrashedException
        print("[SPECTATOR] - Match has started")
        break


def get_current_game_version():
    r = requests.get('https://raw.githubusercontent.com/CommunityDragon/Data/master/patches.json')
    version = r.json()['patches'][-1]['name']
    return version


# def handle(summoner_name):
#     pass


def wait_seconds(WAIT_TIME):
    # print(f"[SPECTATOR] - Waiting {WAIT_TIME}")
    time.sleep(WAIT_TIME)


def find_and_launch_game(match):
    match_id = match.get('id')
    region = match.get('region')

    replay_command = opgg_extractor.get_game_bat(match_id, region)
    # replay_command = r.text

    with open(BAT_PATH, 'w') as f:
        f.write(replay_command)

    subprocess.call([BAT_PATH], stdout=subprocess.DEVNULL)


@pretty_log
def get_video_path():
    from pathlib import Path

    video_paths = sorted(Path(VIDEOS_PATH).iterdir(), key=os.path.getmtime)

    video_path = video_paths[-1]

    return str(video_path)


def handle_game(match_info):
    obs_manager.start()
    enable_settings()

    time.sleep(WAIT_TIME)

    find_and_launch_game(match_info)
    wait_for_game_launched()
    time.sleep(WAIT_TIME)

    replay_api_manager.enable_recording_settings()
    wait_seconds(WAIT_TIME)

    wait_for_game_start()
    player_champion = match_info.get('player_champion')

    match_info['skin_name'] = replay_api_manager.get_player_skin(player_champion)
    match_info['runes'] = replay_api_manager.get_player_runes(player_champion)
    match_info['summonerSpells'] = replay_api_manager.get_player_summoner_spells(player_champion)
    player_position = get_player_position(player_champion)

    league_manager.select_summoner(player_position)
    league_manager.enable_runes()
    wait_seconds(WAIT_TIME)

    league_manager.toggle_recording()

    wait_seconds(WAIT_TIME)

    match_info['path'] = get_video_path()

    wait_seconds(WAIT_TIME)

    wait_finish()
    match_info['items'] = replay_api_manager.get_player_items(player_champion)

    league_manager.toggle_recording()

    close_programs()


def close_programs():
    obs_manager.close_obs()
    league_manager.close_game()
    disable_settings()


def handle_postgame(match_info):
    wait_seconds(WAIT_TIME * 5)
    upload_manager.add_video_to_queue(match_info)


def get_summoner_current_match(summoner):
    tries = 3
    while tries > 0:
        try:
            match = summoner.current_match
            return match
        except datapipelines.common.NotFoundError:
            tries -= 1
            time.sleep(1)


def get_tier_lp_from_rank(rank):
    p = re.compile("([a-zA-Z]*) \\(([0-9]*) LP\\)")
    result = p.search(rank)
    tier = result.group(1)
    lp = result.group(2)
    return tier, lp


def spectate(match_data):
    close_programs()

    region = match_data.get('region')
    summoner_name = match_data.get('summoner_name')

    summoner = cass.get_summoner(region=region, name=summoner_name)
    match = get_summoner_current_match(summoner)

    if match is None:
        print(f'"{summoner_name}" is not in game')
        return
    match_id = match.id

    players_data = match_data.get('players_data')

    player_data = players_data[summoner_name]
    player_position = list(players_data.keys()).index(summoner_name)
    player_champion = player_data['champion']

    enemy_position = (player_position + 5) % 10
    enemy_summoner_name = list(players_data.keys())[enemy_position]
    enemy_champion = players_data[enemy_summoner_name].get('champion')

    role = player_data.get('role')
    rank = player_data.get('rank')
    tier, lp = get_tier_lp_from_rank(rank)
    version = get_current_game_version()

    match_info = {
        'players_data': players_data,
        'player_champion': player_champion,
        'role': role,
        'summoner_name': summoner_name,
        'enemy_champion': enemy_champion,
        'region': region,
        'rank': rank,
        'version': version,
        'id': match_id,
    }

    print(f"[SPECTATOR] - Spectating {get_title(match_info)}")

    try:
        handle_game(match_info)
    except (subprocess.CalledProcessError, requests.exceptions.ConnectionError, GameCrashedException, PortNotFoundException) as e:

        print(f'{e} was raised during the process')
        kill_bugsplat()
        close_programs()
        wait_seconds(WAIT_TIME)

        if 'path' in match_info:
            os.remove(match_info['path'])
            print(f"{match_info['path']} Removed!")
        return

    metadata = {
        'description': description_builder.get_description(match_info),
        'tags': tags_builder.get_tags(match_info),
        'title': get_title(match_info),
        'player_champion': player_champion,
        'skin_name': match_info['skin_name'],
        'items': match_info['items'],
        'runes': match_info['runes'],
        'summonerSpells': match_info['summonerSpells'],
        'path': match_info['path'],
        'region': region,
        'tier': tier,
        'lp': lp,
        'role': role,

    }
    handle_postgame(metadata)