"""Fetch prediction ("voorspellingen") and result data for one or more
Scorito poules (private pools) of the same tournament, and save it as
structured JSON for a local dashboard to consume.

Endpoints used (reverse-engineered from the Scorito Gamecenter web app):
- ranking.scorito.com              -> poule participants, per-round scores
- football.scorito.com             -> tournament match schema, results, lineups, goals
- ftm-prediction-query.scorito.com -> each participant's predictions per round

A poule (pool) id only selects WHICH roster of participants to rank -- the
match schema, results, goals, players, and each user's own predictions and
round scores are all scoped to the market/event (or the user), not the
poule. So everything except the roster itself is fetched ONCE and shared
across every poule in pools.json, rather than being re-fetched per poule.

Writes:
- data/shared.json      -> matches, players, group standings, round scores
                            (identical for every poule of this tournament)
- data/predictions.json -> predictions/topscorer/champion picks, for the
                            UNION of participants across all poules
- data/pools/{slug}.json -> one file per poule: its own roster (userId,
                            name, rank, totalPoints within that poule)
"""
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scorito_scraper")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
POOLS_DATA_DIR = DATA_DIR / "pools"
SHARED_FILE = DATA_DIR / "shared.json"
PREDICTIONS_FILE = DATA_DIR / "predictions.json"
POOLS_CONFIG_FILE = ROOT / "pools.json"
REQUEST_DELAY_SECONDS = 0.3

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Scorito labels knockout rounds by "EventRoundOrder" (group stage matchdays
# are 1-3, then the knockout bracket continues 4-9). Group stage matchdays
# are THREE INDEPENDENT rounds in Scorito's own scoring (confirmed via the
# Gamecenter UI's own round dropdown: "Ronde 1"/"Ronde 2"/"Ronde 3", each with
# its own score -- NOT a single cumulative "Groepsfase" total). Ronde 3's own
# total also includes the group-position bonus, since that's when groups
# finalize.
ROUND_NAMES = {
    1: "Ronde 1",
    2: "Ronde 2",
    3: "Ronde 3",
    4: "Laatste 32",
    5: "1/8e finale",
    6: "Kwartfinale",
    7: "Halve finale",
    8: "Troostfinale",
    9: "Finale",
}

GOAL_EVENT_TYPE = 1

# The pool's point table scales 1x/2x/3x/4x/5x/6x from the group-stage base
# across group/R32/R16/QF/SF/final+3rd -- both match-point tiers AND, as
# discovered directly from Scorito's API, topscorer-prediction ids follow
# this same 6-tier scheme (topscorer id = offset + tier). Topscorer picks
# can be changed every round, unlike the champion pick (made once).
ROUND_TIER = {1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 6}
TIER_NAMES = {
    1: "Groepsfase",
    2: "Laatste 32",
    3: "1/8e finale",
    4: "Kwartfinale",
    5: "Halve finale",
    6: "Finale / Troostfinale",
}

# Scorito's player position field is a bitmask, confirmed empirically against
# known players (Alisson=1, Van Dijk=2, Kakuta=4, Messi/Mbappe/Haaland=8) and
# against the full 1469-player list (only these four values ever occur).
# Keeper and defender share one topscorer-bonus tier in this pool's rules.
POSITION_CATEGORY = {1: "defenderKeeper", 2: "defenderKeeper", 4: "midfielder", 8: "attacker"}


class Config:
    def __init__(self):
        load_dotenv()
        self.token = os.getenv("SCORITO_BEARER_TOKEN")
        self.market_id = os.getenv("SCORITO_MARKET_ID")
        self.event_id = os.getenv("SCORITO_EVENT_ID")
        self.round_id_offset = int(os.getenv("SCORITO_ROUND_ID_OFFSET", "7161"))
        self.topscorer_id_offset = int(os.getenv("SCORITO_TOPSCORER_ID_OFFSET", "457"))

        missing = [
            name
            for name, value in [
                ("SCORITO_BEARER_TOKEN", self.token),
                ("SCORITO_MARKET_ID", self.market_id),
                ("SCORITO_EVENT_ID", self.event_id),
            ]
            if not value
        ]
        if missing:
            log.error(
                "Missing required .env values: %s. "
                "Copy .env.example to .env and fill in your values.",
                ", ".join(missing),
            )
            sys.exit(1)

    def headers(self) -> dict:
        headers = dict(BROWSER_HEADERS)
        headers["Authorization"] = f"Bearer {self.token}"
        return headers


def load_pools() -> list[dict]:
    """Every poule (Scorito's private pool) this dashboard tracks -- see
    pools.json. Each needs only a slug (used as a stable id/URL key), a
    display name, and its Scorito pool id."""
    with POOLS_CONFIG_FILE.open(encoding="utf-8") as f:
        pools = json.load(f)
    missing = [p["slug"] for p in pools if not p.get("poolId")]
    if missing or not pools:
        log.error("pools.json is missing or has entries without a poolId: %s", pools)
        sys.exit(1)
    return pools


MAX_RETRIES = 3


def get_json(url: str, headers: dict):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.exceptions.RequestException as exc:
            # Scrapes now run 50+ unique users over many minutes -- a
            # transient connection reset shouldn't blow away that whole
            # run, so retry a couple of times with backoff before giving up.
            if attempt == MAX_RETRIES:
                raise
            wait = REQUEST_DELAY_SECONDS * (2**attempt)
            log.warning(
                "Request failed (%s), retrying in %.1fs (attempt %d/%d): %s",
                exc, wait, attempt, MAX_RETRIES, url,
            )
            time.sleep(wait)
            continue

        if response.status_code == 401:
            log.error(
                "401 Unauthorized on %s: the Bearer token has expired or is invalid. "
                "Grab a fresh token from DevTools and update your .env file.",
                url,
            )
            sys.exit(1)

        response.raise_for_status()
        time.sleep(REQUEST_DELAY_SECONDS)

        body = response.json()
        return body.get("Content", body.get("content", []))


# ftm-prediction-query hosts each user's predictions behind one of several
# API versions, seemingly per-user (looks like a migration/sharding artifact:
# e.g. one user's data lives under /1/, another's under /3/, another's under
# /6/, regardless of who's asking or which round). Querying the wrong version
# returns an empty result rather than an error, so we just try each version
# and use whichever responds with data. A per-user cache avoids re-probing
# every version for every round once we know which one a user lives on.
PREDICTION_API_VERSIONS = ("1", "2", "3", "4", "5", "6")


def get_json_any_version(
    url_template: str,
    headers: dict,
    version_cache: dict,
    cache_key,
    is_valid=bool,
):
    """url_template must contain a single {version} placeholder.

    `is_valid` decides whether a response counts as "found the right version"
    -- plain truthiness isn't enough for endpoints that return a non-empty
    dict shell (e.g. {"teamPlayerEnrichedIds": []}) even when queried under
    the wrong version.
    """
    ordered_versions = PREDICTION_API_VERSIONS
    hint = version_cache.get(cache_key)
    if hint:
        ordered_versions = (hint,) + tuple(v for v in PREDICTION_API_VERSIONS if v != hint)

    for version in ordered_versions:
        content = get_json(url_template.format(version=version), headers)
        if is_valid(content):
            version_cache[cache_key] = version
            return content
    return []


def round_id_for_order(cfg: Config, order: int) -> int:
    # Each EventRoundOrder -- including the three group-stage matchdays -- is
    # its own independent round id. Confirmed directly against the
    # Gamecenter UI's own round dropdown ("Ronde 1"/"Ronde 2"/"Ronde 3" are
    # separate entries with separate scores, not one cumulative "Groepsfase"
    # total). Predictions happen to be identical across all three (verified
    # empirically), so fetching each independently just costs a few redundant
    # calls; round scores and match/goal details are NOT identical per
    # matchday and must be fetched per round id or they go silently missing.
    return cfg.round_id_offset + order


def round_label_for_id(cfg: Config, round_id: int) -> str:
    order = round_id - cfg.round_id_offset
    return ROUND_NAMES.get(order, f"Ronde {order}")


def fetch_participants(cfg: Config, pool_id: str) -> list[dict]:
    """Every participant in a poule, paginated 10-at-a-time.

    The endpoint is getpage/{poolId}/{selectedRankType}/{pageIndex} -- the
    "0" right after the pool id is NOT a page index (a stray easy
    assumption, since it happens to also accept 0/1): it's a rank-type
    selector (0 = "Totaal", 1 = "Ronde", matching the site's own toggle),
    confirmed by the 400 error it throws for any other value ("selectedRankType
    ... is invalid"). The REAL page index is the next segment, still 10 items
    per page -- confirmed against ParticipantCount (e.g. a 26-person poule
    needs pages 0/1/2, with page 2 returning the last 6 and page 3 empty).
    Silently trusting just the first page under-reported every poule with
    more than 10 members.
    """
    participants: list[dict] = []
    page = 0
    while True:
        url = f"https://ranking.scorito.com/9/ranking/v2.0/gameranking/getpage/{pool_id}/0/{page}"
        content = get_json(url, cfg.headers())
        items = content.get("RankingItems", []) if isinstance(content, dict) else []
        if not items:
            break
        participants.extend(
            {
                "userId": item["UserId"],
                "userName": item["UserName"],
                "rank": item.get("Rank"),
                "totalPoints": item.get("TotalPoints"),
            }
            for item in items
        )
        expected_total = content.get("ParticipantCount") if isinstance(content, dict) else None
        if expected_total is not None and len(participants) >= expected_total:
            break
        page += 1

    log.info("Found %d participants in poule %s", len(participants), pool_id)
    return participants


def fetch_match_schema(cfg: Config) -> list[dict]:
    """Canonical match schema: matchId, real home/away teams, real results."""
    matches = []
    for phase in (1, 2):  # 1 = group stage, 2 = knockout stage
        url = f"https://football.scorito.com/footballGeneric/v2.0/matches/koschema/{cfg.event_id}/{phase}"
        content = get_json(url, cfg.headers())
        for m in content:
            matches.append(
                {
                    "matchId": m["MatchId"],
                    "roundOrder": m["EventRoundOrder"],
                    "roundName": ROUND_NAMES.get(
                        m["EventRoundOrder"], f"Ronde {m['EventRoundOrder']}"
                    ),
                    # Scorito returns UTC timestamps with no timezone marker
                    # (e.g. "2026-06-24T02:00:00") -- append "Z" so it's
                    # unambiguous UTC for any consumer (JS Date, Python
                    # datetime.fromisoformat), instead of silently being
                    # parsed as local time.
                    "matchDate": m["MatchDate"] + "Z",
                    "homeTeam": m["HomeTeamNameShort"],
                    "awayTeam": m["AwayTeamNameShort"],
                    "homeTeamId": m["HomeTeamId"],
                    "awayTeamId": m["AwayTeamId"],
                    "roundWinnerType": m["RoundWinnerType"],
                    "homeScore": m["HomeScore"],
                    "awayScore": m["AwayScore"],
                    "status": m["Status"],
                }
            )
    log.info("Found %d tournament matches", len(matches))
    return matches


def fetch_group_standings(cfg: Config) -> list[dict]:
    """Real final group-stage standings, for scoring the "correct position per
    team" bonus -- this isn't a separate prediction to fetch; it's derived by
    comparing each participant's group-match predictions (already collected)
    against this real table, done in compute_scores.py.
    """
    url = f"https://football.scorito.com/footballGeneric/v2.0/eventrankings/{cfg.event_id}"
    content = get_json(url, cfg.headers())
    standings = []
    for group in content:
        m = re.search(r"Group\.(\d+)$", group.get("TranslatedName", ""))
        group_number = int(m.group(1)) if m else None
        for entry in group.get("RankEntries", []):
            standings.append(
                {
                    "groupNumber": group_number,
                    "teamId": entry["TeamId"],
                    "teamName": entry["TranslatedName"],
                    "actualRank": entry["Rank"],
                    "points": entry["Points"],
                    "goalsFor": entry["GoalsFor"],
                    "goalsAgainst": entry["GoalsAgainst"],
                }
            )
    log.info("Found final standings for %d teams across %d groups", len(standings), len(content))
    return standings


def fetch_predictions(
    cfg: Config,
    participants: list[dict],
    round_orders: list[int],
    version_cache: dict,
) -> list[dict]:
    round_ids = sorted({round_id_for_order(cfg, order) for order in round_orders})
    predictions = []

    for participant in participants:
        user_id = participant["userId"]
        for round_id in round_ids:
            url = (
                "https://ftm-prediction-query.scorito.com/{version}/v1.0/matchprediction/"
                f"{round_id}/{user_id}"
            )
            content = get_json_any_version(url, cfg.headers(), version_cache, user_id)
            for p in content:
                predictions.append(
                    {
                        "userId": p["userId"],
                        "userName": participant["userName"],
                        "matchId": p["matchId"],
                        "homeScorePredicted": p["homeScore"],
                        "awayScorePredicted": p["awayScore"],
                        "dateTimePredicted": p["dateTimePredicted"],
                    }
                )

    log.info(
        "Collected %d predictions across %d participants",
        len(predictions),
        len(participants),
    )
    return predictions


def fetch_topscorer_predictions(
    cfg: Config, participants: list[dict], round_orders: list[int], version_cache: dict
) -> list[dict]:
    """Each user's topscorer pick PER ROUND TIER -- confirmed empirically that
    picks can change every round (topscorer id = topscorer_id_offset + tier,
    the same 1-6 tier scheme as match points)."""
    tiers = sorted({ROUND_TIER[order] for order in round_orders})
    predictions = []

    for tier in tiers:
        topscorer_id = cfg.topscorer_id_offset + tier
        for participant in participants:
            user_id = participant["userId"]
            url = (
                "https://ftm-prediction-query.scorito.com/{version}/v1.0/topscorerprediction/"
                f"{topscorer_id}/{user_id}"
            )
            content = get_json_any_version(
                url,
                cfg.headers(),
                version_cache,
                user_id,
                is_valid=lambda c: bool(c.get("teamPlayerEnrichedIds")) if c else False,
            )
            if not content:
                continue
            predictions.append(
                {
                    "userId": content["userId"],
                    "userName": participant["userName"],
                    "tier": tier,
                    "roundName": TIER_NAMES.get(tier, f"Tier {tier}"),
                    "playerIds": content["teamPlayerEnrichedIds"],
                    "dateTimePredicted": content["dateTimePredicted"],
                }
            )

    log.info(
        "Collected %d topscorer predictions across %d round tiers",
        len(predictions),
        len(tiers),
    )
    return predictions


def fetch_match_details(cfg: Config, round_ids: list[int]):
    """Full team names, goal scorers, and a playerId -> name/team directory.

    Sourced from matchoverview, which includes team lineups (so numeric
    player ids -- like the ones in topscorer predictions -- can be resolved
    to real names and teams) and per-match goal events.
    """
    match_extra: dict[int, dict] = {}
    players: dict[int, dict] = {}

    for round_id in round_ids:
        url = f"https://football.scorito.com/footballGeneric/v2.0/matchoverview/marketround/{round_id}"
        content = get_json(url, cfg.headers())

        for m in content:
            home_team = (m.get("HomeTeamLineup") or {}).get("Team") or {}
            away_team = (m.get("AwayTeamLineup") or {}).get("Team") or {}
            home_lineup = (m.get("HomeTeamLineup") or {}).get("Lineup") or []
            away_lineup = (m.get("AwayTeamLineup") or {}).get("Lineup") or []

            for player, team in [(p, home_team) for p in home_lineup] + [
                (p, away_team) for p in away_lineup
            ]:
                players[player["Id"]] = {
                    "playerId": player["Id"],
                    "name": player["NameShort"],
                    "teamId": team.get("Id"),
                    "teamName": team.get("Name"),
                }

            goals = []
            for event in m.get("MatchEvents") or []:
                if event.get("MatchEventType") != GOAL_EVENT_TYPE:
                    continue
                scorer = players.get(event.get("PlayerId"), {})
                goals.append(
                    {
                        "minute": event.get("PlayMinute"),
                        "playerId": event.get("PlayerId"),
                        "playerName": scorer.get("name"),
                        "teamName": scorer.get("teamName"),
                        "homeScoreAfter": event.get("HomeScore"),
                        "awayScoreAfter": event.get("AwayScore"),
                    }
                )

            match_extra[m["Id"]] = {
                "homeTeamFull": home_team.get("Name"),
                "awayTeamFull": away_team.get("Name"),
                "goals": goals,
            }

    log.info(
        "Fetched match details (players: %d, matches enriched: %d)",
        len(players),
        len(match_extra),
    )
    return match_extra, players


def fetch_player_positions(cfg: Config) -> dict[int, dict]:
    """playerId -> {category, enrichedId}.

    Topscorer predictions identify players by "TeamPlayerEnrichedId", a
    completely different id space from the "PlayerId" used everywhere else
    (match lineups, goal events). Without this mapping, goal scorers can
    never be matched against anyone's topscorer pick.
    """
    url = f"https://ftm-query.scorito.com/player/v1.0/{cfg.market_id}"
    content = get_json(url, cfg.headers())
    info = {}
    for p in content:
        category = POSITION_CATEGORY.get(p.get("Position"))
        info[p["PlayerId"]] = {
            "category": category,
            "enrichedId": p.get("TeamPlayerEnrichedId"),
        }
    log.info("Resolved position/enrichedId for %d players", len(info))
    return info


def fetch_champion_predictions(
    cfg: Config, participants: list[dict], version_cache: dict
) -> list[dict]:
    """Each user's one-time pick for the tournament winner (resolves at the final)."""
    predictions = []

    for participant in participants:
        user_id = participant["userId"]
        url = (
            "https://ftm-prediction-query.scorito.com/{version}/v1.0/championprediction/"
            f"{cfg.market_id}/{user_id}"
        )
        content = get_json_any_version(
            url,
            cfg.headers(),
            version_cache,
            user_id,
            is_valid=lambda c: bool(c.get("teamId")) if c else False,
        )
        if not content:
            continue
        predictions.append(
            {
                "userId": content["userId"],
                "userName": participant["userName"],
                "teamId": content["teamId"],
                "dateTimePredicted": content["dateTimePredicted"],
            }
        )

    log.info("Collected %d champion predictions", len(predictions))
    return predictions


def fetch_round_scores(
    cfg: Config, participants: list[dict], round_orders: list[int]
) -> list[dict]:
    """Per-round points per participant, for a cumulative 'line race' chart."""
    round_ids = sorted({round_id_for_order(cfg, order) for order in round_orders})
    scores = []

    for round_id in round_ids:
        round_name = round_label_for_id(cfg, round_id)
        for participant in participants:
            url = (
                "https://ranking.scorito.com/3/ranking/v2.0/scoreblock/userscore/"
                f"{cfg.market_id}/{round_id}/{participant['userId']}"
            )
            content = get_json(url, cfg.headers())
            if not content:
                continue
            scores.append(
                {
                    "userId": participant["userId"],
                    "userName": participant["userName"],
                    "roundId": round_id,
                    "roundName": round_name,
                    "points": content.get("Points"),
                    "betterThanPercentage": content.get("BetterThanPercentage"),
                }
            )

    log.info("Collected %d round scores", len(scores))
    return scores


def save(data: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Saved data to %s", output_path)


def main() -> None:
    cfg = Config()
    pools = load_pools()

    try:
        # 1. Each poule's own roster -- the only thing that's actually
        # poule-specific. Save these first so a later failure still leaves
        # useful per-poule output.
        rosters: dict[str, list[dict]] = {}
        for pool in pools:
            rosters[pool["slug"]] = fetch_participants(cfg, pool["poolId"])

        # 2. Union of every unique participant across all poules -- match
        # predictions, topscorer picks, champion picks, and round scores are
        # all scoped to the user (or market), not the poule, so each unique
        # user only needs to be fetched once even if they're in several poules.
        all_participants_by_id: dict[int, dict] = {}
        for roster in rosters.values():
            for p in roster:
                all_participants_by_id.setdefault(p["userId"], p)
        all_participants = list(all_participants_by_id.values())
        log.info(
            "%d unique participants across %d poules", len(all_participants), len(pools)
        )

        # 3. Tournament-wide data (shared by every poule).
        matches = fetch_match_schema(cfg)
        round_orders = sorted({m["roundOrder"] for m in matches})
        round_ids = sorted({round_id_for_order(cfg, order) for order in round_orders})

        version_cache: dict[int, str] = {}
        predictions = fetch_predictions(cfg, all_participants, round_orders, version_cache)
        topscorer_predictions = fetch_topscorer_predictions(
            cfg, all_participants, round_orders, version_cache
        )
        champion_predictions = fetch_champion_predictions(
            cfg, all_participants, version_cache
        )
        match_extra, players = fetch_match_details(cfg, round_ids)
        player_categories = fetch_player_positions(cfg)
        round_scores = fetch_round_scores(cfg, all_participants, round_orders)
        group_standings = fetch_group_standings(cfg)
    except requests.exceptions.RequestException as exc:
        log.error("Request failed: %s", exc)
        sys.exit(1)

    for match in matches:
        extra = match_extra.get(match["matchId"], {})
        match["homeTeamFull"] = extra.get("homeTeamFull")
        match["awayTeamFull"] = extra.get("awayTeamFull")
        match["goals"] = extra.get("goals", [])

    for player in players.values():
        info = player_categories.get(player["playerId"], {})
        player["category"] = info.get("category")
        player["enrichedId"] = info.get("enrichedId")

    save(
        {
            "marketId": cfg.market_id,
            "eventId": cfg.event_id,
            "matches": matches,
            "players": list(players.values()),
            "roundScores": round_scores,
            "groupStandings": group_standings,
        },
        SHARED_FILE,
    )

    save(
        {
            "predictions": predictions,
            "topScorerPredictions": topscorer_predictions,
            "championPredictions": champion_predictions,
        },
        PREDICTIONS_FILE,
    )

    for pool in pools:
        save(
            {
                "slug": pool["slug"],
                "name": pool["name"],
                "poolId": pool["poolId"],
                "marketId": cfg.market_id,
                "eventId": cfg.event_id,
                "participants": rosters[pool["slug"]],
            },
            POOLS_DATA_DIR / f"{pool['slug']}.json",
        )


if __name__ == "__main__":
    main()
