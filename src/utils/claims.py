from shapely.geometry import shape, Polygon
from shapely.ops import unary_union
from database.db import db
from utils.utils import haversine_distance
from config.config import MIN_POLYGON_AREA_M2, MAX_CLOSE_DISTANCE_M
from datetime import datetime, timezone
from typing import Optional, Dict, Any, cast
import uuid
import logging
import math

logger = logging.getLogger(__name__)

BADGES = [
    {"badge_id": "first_run", "name": "First Steps", "description": "Complete your first run", "icon": "footprints", "criteria": {"total_runs": 1}},
    {"badge_id": "area_100", "name": "Territory Starter", "description": "Claim 100 sq meters", "icon": "map", "criteria": {"total_area_m2": 100}},
    {"badge_id": "area_1000", "name": "Land Baron", "description": "Claim 1,000 sq meters", "icon": "crown", "criteria": {"total_area_m2": 1000}},
    {"badge_id": "area_10000", "name": "Territory King", "description": "Claim 10,000 sq meters", "icon": "trophy", "criteria": {"total_area_m2": 10000}},
    {"badge_id": "distance_5k", "name": "5K Runner", "description": "Run 5km total", "icon": "running", "criteria": {"total_distance_m": 5000}},
    {"badge_id": "distance_marathon", "name": "Marathon Legend", "description": "Run 42.195km total", "icon": "medal", "criteria": {"total_distance_m": 42195}},
    {"badge_id": "streak_7", "name": "Week Warrior", "description": "7 day running streak", "icon": "fire", "criteria": {"current_streak": 7}},
    {"badge_id": "streak_30", "name": "Unstoppable", "description": "30 day running streak", "icon": "zap", "criteria": {"current_streak": 30}},
    {"badge_id": "runs_10", "name": "Dedicated", "description": "Complete 10 runs", "icon": "star", "criteria": {"total_runs": 10}},
    {"badge_id": "runs_100", "name": "Century Club", "description": "Complete 100 runs", "icon": "award", "criteria": {"total_runs": 100}},
]


async def detect_and_claim_loop(run: Dict[str, Any], user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    points = run.get("points", [])
    if len(points) < 10:
        return None

    coords = [(p["lon"], p["lat"]) for p in points]
    last_point = coords[-1]
    loop_start_idx = None

    for i in range(len(coords) - 10):
        dist = haversine_distance(coords[i][1], coords[i][0], last_point[1], last_point[0])
        if dist <= MAX_CLOSE_DISTANCE_M:
            loop_start_idx = i
            break

    if loop_start_idx is None:
        return None

    loop_coords = coords[loop_start_idx:]
    loop_coords.append(loop_coords[0])

    if len(loop_coords) < 4:
        return None

    try:
        polygon = Polygon(loop_coords)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if not polygon.is_valid or polygon.is_empty:
            return None

        centroid = polygon.centroid
        area_m2 = polygon.area * 111320 * 111320 * math.cos(math.radians(centroid.y))
        if area_m2 < MIN_POLYGON_AREA_M2:
            return None

        await handle_claim_conflicts(polygon, user["user_id"])

        claim_id = f"claim_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        claim_doc = {
            "claim_id": claim_id,
            "owner_id": user["user_id"],
            "owner_name": user["name"],
            "geometry": {"type": "Polygon", "coordinates": [list(polygon.exterior.coords)]},
            "area_m2": area_m2,
            "center_lat": centroid.y,
            "center_lon": centroid.x,
            "created_at": now,
            "last_maintained_at": now,
            "source_run_id": run["run_id"]
        }

        await db.claims.insert_one(claim_doc)
        await db.runs.update_one({"run_id": run["run_id"]}, {"$push": {"claims": claim_id}, "$inc": {"area_claimed_m2": area_m2}})

        return {"claim_id": claim_id, "area_m2": area_m2, "geometry": claim_doc["geometry"], "message": f"Claimed {area_m2:.1f} sq meters!"}
    except Exception as e:
        logger.error(f"Error creating claim: {e}")
        return None


async def handle_claim_conflicts(new_polygon: Polygon, user_id: str) -> None:
    bounds = new_polygon.bounds
    existing_claims = await db.claims.find({
        "center_lon": {"$gte": bounds[0] - 0.01, "$lte": bounds[2] + 0.01},
        "center_lat": {"$gte": bounds[1] - 0.01, "$lte": bounds[3] + 0.01},
        "owner_id": {"$ne": user_id}
    }, {"_id": 0}).to_list(100)

    for claim in existing_claims:
        try:
            existing_polygon = shape(claim["geometry"])
            if new_polygon.intersects(existing_polygon):
                intersection = new_polygon.intersection(existing_polygon)
                overlap_ratio = intersection.area / existing_polygon.area
                if overlap_ratio > 0.3:
                    remaining = existing_polygon.difference(new_polygon)
                    if remaining.is_empty or remaining.area < MIN_POLYGON_AREA_M2 / (111320 * 111320):
                        await db.claims.delete_one({"claim_id": claim["claim_id"]})
                    else:
                        centroid = remaining.centroid
                        new_area = remaining.area * 111320 * 111320 * math.cos(math.radians(centroid.y))
                        await db.claims.update_one({"claim_id": claim["claim_id"]}, {"$set": {"geometry": {"type": "Polygon", "coordinates": [list(cast(Polygon, remaining).exterior.coords)]}, "area_m2": new_area, "center_lat": centroid.y, "center_lon": centroid.x}})
        except Exception as e:
            logger.error(f"Error handling conflict for claim {claim['claim_id']}: {e}")


async def update_streak(user_id: str) -> None:
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        return
    last_run_date = user.get("last_run_date")
    current_streak = user.get("current_streak", 0)
    today = datetime.now(timezone.utc).date()
    if last_run_date:
        if isinstance(last_run_date, str):
            last_run_date = datetime.fromisoformat(last_run_date.replace("Z", "+00:00")).date()
        days_diff = (today - last_run_date).days
        if days_diff == 0:
            return
        elif days_diff == 1:
            current_streak += 1
        else:
            current_streak = 1
    else:
        current_streak = 1
    await db.users.update_one({"user_id": user_id}, {"$set": {"current_streak": current_streak}})


async def check_badges(user_id: str) -> None:
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        return
    current_badges = set(user.get("badges", []))
    new_badges = []
    now = datetime.now(timezone.utc).isoformat()
    for badge in BADGES:
        if badge["badge_id"] in current_badges:
            continue
        criteria_met = True
        for field, value in badge["criteria"].items():
            if user.get(field, 0) < value:
                criteria_met = False
                break
        if criteria_met:
            new_badges.append(badge["badge_id"])
            await db.user_badges.insert_one({"user_id": user_id, "badge_id": badge["badge_id"], "earned_at": now})
    if new_badges:
        await db.users.update_one({"user_id": user_id}, {"$push": {"badges": {"$each": new_badges}}})
