from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Request, Response, Depends
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
sys.path.append(str(Path(__file__).parent))
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional, Dict, Any, cast
import uuid
from datetime import datetime, timezone, timedelta

import json
from shapely.geometry import shape, Polygon, LineString, Point
from shapely.ops import unary_union
import h3
import bcrypt
import jwt
import asyncio

load_dotenv(ROOT_DIR / '.env')

from database.db import db, client
from config.config import (
    JWT_SECRET, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_HOURS,
    MAX_RUNNING_SPEED_MPS, MIN_ACCURACY_M, MIN_POLYGON_AREA_M2, MAX_CLOSE_DISTANCE_M,
    CORS_ORIGINS, H3_RESOLUTION, MIN_HEX_CLAIM_COUNT
)
from auth.auth import hash_password, verify_password, create_access_token, decode_token, get_current_user
from utils.utils import haversine_distance
from utils.claims import BADGES, check_badges, update_streak, detect_and_claim_loop

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown
    client.close()

app = FastAPI(title="Intertia API", lifespan=lifespan)
api_router = APIRouter(prefix="/api")

import traceback
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_msg = f"Global exception: {str(exc)}\n{traceback.format_exc()}"
    print(error_msg)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error", "error": str(exc)},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*"), "Access-Control-Allow-Credentials": "true"}
    )

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===================== MODELS =====================
from database.schemas import (
    UserCreate, UserLogin, UserResponse, TokenResponse,
    RunPointInput, RunStart, RunResponse, ClaimResponse,
    GroupSession, BadgeResponse, SeasonResponse,
    GoogleAuth, ForgotPassword, ResetPassword
)
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# ===================== AUTH HELPERS =====================
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        # Fallback for old plain-text passwords in development DBs
        return password == hashed

def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

async def get_current_user(request: Request) -> Dict[str, Any]:
    # Check cookie first (for session-based auth)
    session_token = request.cookies.get("session_token")
    if session_token:
        session = await db.user_sessions.find_one({"session_token": session_token}, {"_id": 0})
        if session:
            expires_at = session.get("expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > datetime.now(timezone.utc):
                user = await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})
                if user:
                    return user
    
    # Check Authorization header (for JWT)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        user_id = decode_token(token)
        if user_id:
            user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
            if user:
                return user
    
    raise HTTPException(status_code=401, detail="Not authenticated")

# ===================== AUTH ENDPOINTS =====================
@api_router.post("/auth/register", response_model=TokenResponse)
async def register(user_data: UserCreate) -> TokenResponse:
    existing = await db.users.find_one({"email": user_data.email}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    
    user_doc = {
        "user_id": user_id,
        "email": user_data.email,
        "name": user_data.name,
        "password_hash": hash_password(user_data.password),
        "picture": None,
        "total_distance_m": 0,
        "total_area_m2": 0,
        "total_runs": 0,
        "current_streak": 0,
        "last_run_date": None,
        "badges": [],
        "created_at": now
    }
    await db.users.insert_one(user_doc)
    
    token = create_access_token(user_id)
    user_response = UserResponse(**{k: v for k, v in user_doc.items() if k != "password_hash"})
    return TokenResponse(access_token=token, user=user_response)

@api_router.post("/auth/login", response_model=TokenResponse)
async def login(credentials: UserLogin) -> TokenResponse:
    user = await db.users.find_one({"email": credentials.email}, {"_id": 0})
    
    # Check both password_hash and password (for older DB records)
    hashed_pass = user.get("password_hash") or user.get("password") or ""
    
    if not user or not verify_password(credentials.password, hashed_pass):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = create_access_token(user["user_id"])
    user_response = UserResponse(**{k: v for k, v in cast(Dict[str, Any], user).items() if k != "password_hash"})
    return TokenResponse(access_token=token, user=user_response)


@api_router.post("/auth/google", response_model=TokenResponse)
async def google_login(auth_data: GoogleAuth) -> TokenResponse:
    try:
        # Note: In production, pass the exact CLIENT_ID to verify_oauth2_token.
        # We pass None here to allow any client ID for dev purposes, but it still verifies Google's signature.
        idinfo = id_token.verify_oauth2_token(auth_data.credential, google_requests.Request(), None)
        email = idinfo['email']
        name = idinfo.get('name', email.split('@')[0])
        picture = idinfo.get('picture', None)

        user = await db.users.find_one({"email": email}, {"_id": 0})
        if not user:
            # Create user if it doesn't exist
            user_id = f"user_{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc).isoformat()
            user_doc = {
                "user_id": user_id,
                "email": email,
                "name": name,
                "password_hash": "", # No password for Google users
                "picture": picture,
                "total_distance_m": 0,
                "total_area_m2": 0,
                "total_runs": 0,
                "current_streak": 0,
                "last_run_date": None,
                "badges": [],
                "created_at": now
            }
            await db.users.insert_one(user_doc)
            user = user_doc

        token = create_access_token(user["user_id"])
        user_response = UserResponse(**{k: v for k, v in cast(Dict[str, Any], user).items() if k != "password_hash"})
        return TokenResponse(access_token=token, user=user_response)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")


@api_router.post("/auth/forgot-password")
async def forgot_password(data: ForgotPassword) -> Dict[str, str]:
    user = await db.users.find_one({"email": data.email})
    if not user:
        # Don't reveal if email exists or not
        return {"message": "If an account with that email exists, a reset token has been generated."}
    
    # Generate a simple 6-digit code for local dev instead of email
    import random
    reset_code = str(random.randint(100000, 999999))
    expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    
    await db.users.update_one(
        {"email": data.email},
        {"$set": {"reset_token": reset_code, "reset_token_expires": expire.isoformat()}}
    )
    
    # In a real app, send an email. For dev, we return the token in the response so the UI can prefill it.
    return {"message": "Reset code generated", "dev_token": reset_code}


@api_router.post("/auth/reset-password")
async def reset_password(data: ResetPassword) -> Dict[str, str]:
    user = await db.users.find_one({"reset_token": data.token})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        
    expires_at = datetime.fromisoformat(user["reset_token_expires"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
        
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="Reset token expired")
        
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {"password_hash": hash_password(data.new_password)},
            "$unset": {"reset_token": "", "reset_token_expires": ""}
        }
    )
    
    return {"message": "Password updated successfully"}



@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(request: Request) -> UserResponse:
    user = await get_current_user(request)
    return UserResponse(**{k: v for k, v in user.items() if k != "password_hash"})

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response) -> Dict[str, str]:
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    response.delete_cookie("session_token", path="/")
    return {"message": "Logged out"}

# ===================== RUN ENDPOINTS =====================
@api_router.post("/runs/start", response_model=RunResponse)
async def start_run(run_data: RunStart, request: Request) -> RunResponse:
    user = await get_current_user(request)
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    
    run_doc = {
        "run_id": run_id,
        "user_id": user["user_id"],
        "status": "active",
        "run_type": run_data.run_type,
        "group_id": run_data.group_id,
        "started_at": now,
        "ended_at": None,
        "distance_m": 0,
        "area_claimed_m2": 0,
        "points_count": 0,
        "claims": [],
        "points": []
    }
    await db.runs.insert_one(run_doc)
    return RunResponse(**{k: v for k, v in run_doc.items() if k != "points"})

@api_router.post("/runs/{run_id}/points")
async def add_run_points(run_id: str, points: List[RunPointInput], request: Request) -> Dict[str, Any]:
    user = await get_current_user(request)
    run = await db.runs.find_one({"run_id": run_id, "user_id": user["user_id"]}, {"_id": 0})
    
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "active":
        raise HTTPException(status_code=400, detail="Run is not active")
    
    valid_points = []
    total_distance = 0
    last_point = None

    # Get last stored point to anchor distance calc and dedup
    last_stored_timestamp = None
    if run.get("points"):
        last_point = run["points"][-1]
        last_stored_timestamp = last_point.get("timestamp")

    for pt in points:
        # Dedup: skip points already stored (same or older timestamp)
        if last_stored_timestamp and pt.timestamp <= last_stored_timestamp:
            continue

        # Anti-cheat validation
        if pt.accuracy_m > MIN_ACCURACY_M:
            continue  # Skip inaccurate points

        if pt.speed_mps and pt.speed_mps > MAX_RUNNING_SPEED_MPS:
            logger.warning(f"Suspicious speed detected: {pt.speed_mps} m/s for run {run_id}")
            continue

        point_doc = {
            "timestamp": pt.timestamp,
            "lat": pt.lat,
            "lon": pt.lon,
            "accuracy_m": pt.accuracy_m,
            "speed_mps": pt.speed_mps,
            "heading": pt.heading
        }
        valid_points.append(point_doc)

        # Calculate distance from last point
        if last_point:
            dist = haversine_distance(
                last_point["lat"], last_point["lon"],
                pt.lat, pt.lon
            )
            total_distance += dist

        last_point = point_doc
    
    if valid_points:
        await db.runs.update_one(
            {"run_id": run_id},
            {
                "$push": {"points": {"$each": valid_points}},
                "$inc": {
                    "distance_m": total_distance,
                    "points_count": len(valid_points)
                }
            }
        )
    
    return {"added": len(valid_points), "distance_added": total_distance}

@api_router.post("/runs/{run_id}/close-loop")
async def close_loop(run_id: str, request: Request) -> Dict[str, Any]:
    """Attempt to close a loop and claim territory"""
    user = await get_current_user(request)
    run = await db.runs.find_one({"run_id": run_id, "user_id": user["user_id"]}, {"_id": 0})
    
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] != "active":
        raise HTTPException(status_code=400, detail="Run is not active")
    
    points = run.get("points", [])
    if len(points) < 10:
        raise HTTPException(status_code=400, detail="Not enough points to close loop")
    
    # Try to detect and close a loop
    claim_result = await detect_and_claim_loop(run, user)
    
    if claim_result:
        return claim_result
    else:
        raise HTTPException(status_code=400, detail="No valid loop detected")

@api_router.post("/runs/{run_id}/end", response_model=RunResponse)
async def end_run(run_id: str, request: Request) -> RunResponse:
    user = await get_current_user(request)
    run = await db.runs.find_one({"run_id": run_id, "user_id": user["user_id"]}, {"_id": 0})
    
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    
    # Idempotency guard — prevent double-counting on retries
    if run.get("status") == "completed":
        raise HTTPException(status_code=400, detail="Run already ended")

    now = datetime.now(timezone.utc).isoformat()

    # Update streak BEFORE setting last_run_date so it sees the previous run date
    await update_streak(user["user_id"])

    # Update user stats
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {
            "$inc": {
                "total_distance_m": run["distance_m"],
                "total_area_m2": run["area_claimed_m2"],
                "total_runs": 1
            },
            "$set": {"last_run_date": now}
        }
    )

    # Check for badges
    await check_badges(user["user_id"])

    await db.runs.update_one(
        {"run_id": run_id},
        {"$set": {"status": "completed", "ended_at": now}}
    )
    
    updated_run = await db.runs.find_one({"run_id": run_id}, {"_id": 0})
    if updated_run is None:
        raise HTTPException(status_code=404, detail="Run not found after update")
    return RunResponse(**{k: v for k, v in cast(Dict[str, Any], updated_run).items() if k != "points"})

@api_router.get("/runs", response_model=List[RunResponse])
async def get_user_runs(request: Request, limit: int = 20, offset: int = 0) -> List[RunResponse]:
    user = await get_current_user(request)
    runs = await db.runs.find(
        {"user_id": user["user_id"]},
        {"_id": 0, "points": 0}
    ).sort("started_at", -1).skip(offset).limit(limit).to_list(limit)
    return [RunResponse(**r) for r in runs]

@api_router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, request: Request) -> RunResponse:
    user = await get_current_user(request)
    run = await db.runs.find_one({"run_id": run_id, "user_id": user["user_id"]}, {"_id": 0, "points": 0})
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(**run)

# ===================== H3 HEXAGON ENDPOINTS =====================
@api_router.get("/hexagons")
async def get_hexagons(min_lat: float = -90, max_lat: float = 90, min_lon: float = -180, max_lon: float = 180) -> List[Dict[str, Any]]:
    """Get all claimed H3 hexagons within a bounding box"""
    hexagons = await db.hexagons.find(
        {
            "center_lat": {"$gte": min_lat, "$lte": max_lat},
            "center_lon": {"$gte": min_lon, "$lte": max_lon}
        },
        {"_id": 0}
    ).limit(5000).to_list(5000)
    
    # Calculate decay for each hex
    now = datetime.now(timezone.utc)
    for hx in hexagons:
        last_maintained = hx.get("last_maintained_at", hx["claimed_at"])
        if isinstance(last_maintained, str):
            last_maintained = datetime.fromisoformat(last_maintained.replace("Z", "+00:00"))
        if last_maintained.tzinfo is None:
            last_maintained = last_maintained.replace(tzinfo=timezone.utc)
        days_since = (now - last_maintained).days
        hx["decay_percent"] = min(days_since * 5, 100)
    
    return hexagons

@api_router.get("/hexagons/user/{user_id}")
async def get_user_hexagons(user_id: str) -> List[Dict[str, Any]]:
    hexagons = await db.hexagons.find({"owner_id": user_id}, {"_id": 0}).to_list(2000)
    now = datetime.now(timezone.utc)
    for hx in hexagons:
        last_maintained = hx.get("last_maintained_at", hx.get("claimed_at"))
        if isinstance(last_maintained, str):
            last_maintained = datetime.fromisoformat(last_maintained.replace("Z", "+00:00"))
        if last_maintained and last_maintained.tzinfo is None:
            last_maintained = last_maintained.replace(tzinfo=timezone.utc)
        days_since = (now - last_maintained).days if last_maintained else 0
        hx["decay_percent"] = min(days_since * 5, 100)
    return hexagons

@api_router.post("/runs/{run_id}/claim-hexes")
async def claim_hexes_from_run(run_id: str, request: Request) -> Dict[str, Any]:
    """Convert a run's GPS trace into H3 hexagons and claim them"""
    user = await get_current_user(request)
    run = await db.runs.find_one({"run_id": run_id, "user_id": user["user_id"]}, {"_id": 0})
    
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") not in ("active", "completed"):
        raise HTTPException(status_code=400, detail="Run is not in a claimable state")

    points = run.get("points", [])
    if len(points) < 5:
        raise HTTPException(status_code=400, detail="Not enough GPS points to claim territory")
    
    # Convert GPS points to H3 hex indices
    hex_indices = set()
    for pt in points:
        h3_index = h3.latlng_to_cell(pt["lat"], pt["lon"], H3_RESOLUTION)
        hex_indices.add(h3_index)
    
    if len(hex_indices) < MIN_HEX_CLAIM_COUNT:
        raise HTTPException(status_code=400, detail=f"Run covers only {len(hex_indices)} hexes, need at least {MIN_HEX_CLAIM_COUNT}")
    
    now = datetime.now(timezone.utc).isoformat()
    newly_claimed_count = 0
    flipped_count = 0
    newly_claimed_area_m2 = 0  # Only area from new + flipped hexes (not maintenance)
    total_hex_area_m2 = 0      # Total area of all touched hexes (for run record)
    newly_claimed_hexes = []

    for h3_index in hex_indices:
        # Get hex center for storage
        center_lat, center_lon = h3.cell_to_latlng(h3_index)
        # Get hex boundary for area calculation
        boundary = h3.cell_to_boundary(h3_index)
        hex_area = h3.cell_area(h3_index, unit='m^2')
        total_hex_area_m2 += hex_area

        # Check if hex already claimed
        existing = await db.hexagons.find_one({"h3_index": h3_index}, {"_id": 0})

        if existing:
            if existing["owner_id"] == user["user_id"]:
                # Already own it — refresh maintenance date only, no area credit
                await db.hexagons.update_one(
                    {"h3_index": h3_index},
                    {"$set": {"last_maintained_at": now, "source_run_id": run_id}}
                )
            else:
                # Flip it! Steal from other user
                old_owner = existing["owner_id"]
                await db.hexagons.update_one(
                    {"h3_index": h3_index},
                    {"$set": {
                        "owner_id": user["user_id"],
                        "owner_name": user["name"],
                        "claimed_at": now,
                        "last_maintained_at": now,
                        "source_run_id": run_id
                    }}
                )
                flipped_count += 1
                newly_claimed_count += 1
                newly_claimed_area_m2 += hex_area
                # Reduce old owner's total area
                await db.users.update_one(
                    {"user_id": old_owner},
                    {"$inc": {"total_area_m2": -hex_area}}
                )
        else:
            # New claim
            hex_doc = {
                "h3_index": h3_index,
                "owner_id": user["user_id"],
                "owner_name": user["name"],
                "center_lat": center_lat,
                "center_lon": center_lon,
                "boundary": [list(coord) for coord in boundary],
                "area_m2": hex_area,
                "claimed_at": now,
                "last_maintained_at": now,
                "source_run_id": run_id
            }
            await db.hexagons.insert_one(hex_doc)
            newly_claimed_count += 1
            newly_claimed_area_m2 += hex_area

        newly_claimed_hexes.append({
            "h3_index": h3_index,
            "center_lat": center_lat,
            "center_lon": center_lon
        })

    # Update run record (set, not inc — idempotent on re-claim)
    await db.runs.update_one(
        {"run_id": run_id},
        {"$set": {
            "hex_claims": list(hex_indices),
            "hex_count": len(hex_indices),
            "area_claimed_m2": total_hex_area_m2
        }}
    )

    # Update user's total area — only for hexes newly claimed this call
    if newly_claimed_area_m2 > 0:
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$inc": {"total_area_m2": newly_claimed_area_m2}}
        )

    # Broadcast live event to all dashboard viewers
    await dashboard_manager.broadcast({
        "type": "territory_claimed",
        "user_id": user["user_id"],
        "user_name": user["name"],
        "hex_count": newly_claimed_count,
        "flipped_count": flipped_count,
        "area_m2": newly_claimed_area_m2,
        "hexes": newly_claimed_hexes[:20],  # Limit broadcast payload
        "timestamp": now
    })

    return {
        "claimed_count": newly_claimed_count,
        "flipped_count": flipped_count,
        "total_area_m2": newly_claimed_area_m2,
        "hex_indices": list(hex_indices),
        "message": f"Claimed {newly_claimed_count} new hexagons ({flipped_count} stolen)! Area: {newly_claimed_area_m2:.0f} m²"
    }

# ===================== LEGACY CLAIMS ENDPOINTS =====================
@api_router.get("/claims")
async def get_claims(min_lat: float = -90, max_lat: float = 90, min_lon: float = -180, max_lon: float = 180) -> List[Dict[str, Any]]:
    """Get claims within a bounding box (legacy polygon-based)"""
    claims = await db.claims.find(
        {
            "center_lat": {"$gte": min_lat, "$lte": max_lat},
            "center_lon": {"$gte": min_lon, "$lte": max_lon}
        },
        {"_id": 0}
    ).limit(500).to_list(500)
    
    now = datetime.now(timezone.utc)
    for claim in claims:
        last_maintained = claim.get("last_maintained_at", claim["created_at"])
        if isinstance(last_maintained, str):
            last_maintained = datetime.fromisoformat(last_maintained.replace("Z", "+00:00"))
        if last_maintained.tzinfo is None:
            last_maintained = last_maintained.replace(tzinfo=timezone.utc)
        days_since = (now - last_maintained).days
        claim["decay_percent"] = min(days_since * 5, 100)
    
    return claims

@api_router.get("/claims/user/{user_id}")
async def get_user_claims(user_id: str) -> List[Dict[str, Any]]:
    claims = await db.claims.find({"owner_id": user_id}, {"_id": 0}).to_list(100)
    return claims

# ===================== LEADERBOARDS =====================
@api_router.get("/leaderboards/{metric}")
async def get_leaderboard(metric: str, limit: int = 50, region: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get leaderboard by metric: area, distance, runs, streak"""
    sort_field = {
        "area": "total_area_m2",
        "distance": "total_distance_m",
        "runs": "total_runs",
        "streak": "current_streak"
    }.get(metric, "total_area_m2")
    
    users = await db.users.find(
        {},
        {"_id": 0, "password_hash": 0}
    ).sort(sort_field, -1).limit(limit).to_list(limit)
    
    leaderboard = []
    for i, user in enumerate(users):
        leaderboard.append({
            "rank": i + 1,
            "user_id": user["user_id"],
            "name": user["name"],
            "picture": user.get("picture"),
            "value": user.get(sort_field, 0),
            "metric": metric
        })
    
    return leaderboard

# ===================== BADGES =====================
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

@api_router.get("/badges")
async def get_all_badges() -> List[Dict[str, Any]]:
    return BADGES

@api_router.get("/badges/user/{user_id}")
async def get_user_badges(user_id: str) -> List[Dict[str, Any]]:
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user_badge_ids = user.get("badges", [])
    earned_badges = await db.user_badges.find({"user_id": user_id}, {"_id": 0}).to_list(100)
    earned_map = {b["badge_id"]: b["earned_at"] for b in earned_badges}
    
    result = []
    for badge in BADGES:
        badge_copy = badge.copy()
        badge_copy["earned_at"] = earned_map.get(badge["badge_id"])
        result.append(badge_copy)
    
    return result

# ===================== GROUP SESSIONS =====================
@api_router.post("/groups", response_model=GroupSession)
async def create_group(name: str, request: Request) -> GroupSession:
    user = await get_current_user(request)
    group_id = f"grp_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    
    group_doc = {
        "group_id": group_id,
        "name": name,
        "creator_id": user["user_id"],
        "members": [user["user_id"]],
        "status": "waiting",
        "created_at": now,
        "max_members": 10
    }
    await db.groups.insert_one(group_doc)
    return GroupSession(**group_doc)

@api_router.post("/groups/{group_id}/join")
async def join_group(group_id: str, request: Request) -> Dict[str, Any]:
    user = await get_current_user(request)
    group = await db.groups.find_one({"group_id": group_id}, {"_id": 0})
    
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Group run already started")
    if len(group["members"]) >= group["max_members"]:
        raise HTTPException(status_code=400, detail="Group is full")
    if user["user_id"] in group["members"]:
        raise HTTPException(status_code=400, detail="Already in group")
    
    await db.groups.update_one(
        {"group_id": group_id},
        {"$push": {"members": user["user_id"]}}
    )
    
    return {"message": "Joined group", "group_id": group_id}

@api_router.post("/groups/{group_id}/start")
async def start_group_run(group_id: str, request: Request) -> Dict[str, Any]:
    user = await get_current_user(request)
    group = await db.groups.find_one({"group_id": group_id}, {"_id": 0})
    
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group["creator_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="Only creator can start")
    
    await db.groups.update_one(
        {"group_id": group_id},
        {"$set": {"status": "active"}}
    )
    
    return {"message": "Group run started", "group_id": group_id}

@api_router.get("/groups")
async def get_groups(status: str = "waiting") -> List[Dict[str, Any]]:
    groups = await db.groups.find({"status": status}, {"_id": 0}).limit(50).to_list(50)
    return groups

@api_router.get("/groups/{group_id}")
async def get_group(group_id: str) -> Dict[str, Any]:
    group = await db.groups.find_one({"group_id": group_id}, {"_id": 0})
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group

# ===================== SEASONS =====================
@api_router.get("/seasons/current")
async def get_current_season() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    season = await db.seasons.find_one(
        {
            "start_date": {"$lte": now.isoformat()},
            "end_date": {"$gte": now.isoformat()}
        },
        {"_id": 0}
    )
    
    if not season:
        # Create default season
        season_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        season_end = (season_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
        
        season = {
            "season_id": f"season_{now.strftime('%Y%m')}",
            "name": f"{now.strftime('%B %Y')} Championship",
            "description": "Claim the most territory this month to win!",
            "start_date": season_start.isoformat(),
            "end_date": season_end.isoformat(),
            "prizes": [
                {"rank": 1, "name": "Gold Champion", "badge": "champion_gold"},
                {"rank": 2, "name": "Silver Runner", "badge": "champion_silver"},
                {"rank": 3, "name": "Bronze Warrior", "badge": "champion_bronze"}
            ]
        }
        await db.seasons.insert_one(season)
    
    # Get season leaderboard
    leaderboard = await get_leaderboard("area", limit=10)
    season["leaderboard"] = leaderboard
    
    return season

@api_router.get("/seasons")
async def get_seasons() -> List[Dict[str, Any]]:
    seasons = await db.seasons.find({}, {"_id": 0}).sort("start_date", -1).limit(12).to_list(12)
    return seasons

# ===================== PROFILE =====================
@api_router.get("/users/{user_id}")
async def get_user_profile(user_id: str) -> Dict[str, Any]:
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

@api_router.get("/users/{user_id}/stats")
async def get_user_stats(user_id: str) -> Dict[str, Any]:
    user = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get recent runs
    recent_runs = await db.runs.find(
        {"user_id": user_id, "status": "completed"},
        {"_id": 0, "points": 0}
    ).sort("started_at", -1).limit(5).to_list(5)
    
    # Get hex count (H3-based territory)
    claims_count = await db.hexagons.count_documents({"owner_id": user_id})
    
    # Get rank
    rank_pipeline = [
        {"$sort": {"total_area_m2": -1}},
        {"$group": {"_id": None, "users": {"$push": "$user_id"}}},
        {"$project": {"rank": {"$add": [{"$indexOfArray": ["$users", user_id]}, 1]}}}
    ]
    rank_result = await db.users.aggregate(rank_pipeline).to_list(1)
    rank = rank_result[0]["rank"] if rank_result else 0
    
    return {
        "user_id": user_id,
        "name": user["name"],
        "picture": user.get("picture"),
        "total_distance_m": user.get("total_distance_m", 0),
        "total_area_m2": user.get("total_area_m2", 0),
        "total_runs": user.get("total_runs", 0),
        "current_streak": user.get("current_streak", 0),
        "badges_count": len(user.get("badges", [])),
        "claims_count": claims_count,
        "global_rank": rank,
        "recent_runs": recent_runs
    }

# ===================== WEBSOCKET =====================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, run_id: str) -> None:
        await websocket.accept()
        if run_id not in self.active_connections:
            self.active_connections[run_id] = []
        self.active_connections[run_id].append(websocket)
    
    def disconnect(self, websocket: WebSocket, run_id: str) -> None:
        if run_id in self.active_connections:
            self.active_connections[run_id].remove(websocket)
            if not self.active_connections[run_id]:
                del self.active_connections[run_id]
    
    async def broadcast_to_run(self, run_id: str, message: Dict[str, Any]) -> None:
        if run_id in self.active_connections:
            for connection in self.active_connections[run_id]:
                try:
                    await connection.send_json(message)
                except:
                    pass

manager = ConnectionManager()

# ===================== DASHBOARD LIVE WEBSOCKET =====================
class DashboardManager:
    """Manages global WebSocket connections for the Dashboard live map."""
    def __init__(self):
        self.connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.connections:
            self.connections.remove(websocket)
    
    async def broadcast(self, message: Dict[str, Any]) -> None:
        dead = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

dashboard_manager = DashboardManager()

@app.websocket("/api/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket) -> None:
    """Global WebSocket for live territory events on the Dashboard map."""
    await dashboard_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        dashboard_manager.disconnect(websocket)

@app.websocket("/api/ws/run/{run_id}")
async def websocket_run(websocket: WebSocket, run_id: str) -> None:
    await manager.connect(websocket, run_id)
    try:
        while True:
            data = await websocket.receive_json()
            
            if data.get("type") == "points":
                points = data.get("points", [])
                # Process points (simplified - would need auth in production)
                await manager.broadcast_to_run(run_id, {
                    "type": "position_update",
                    "points": points,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            
            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    
    except WebSocketDisconnect:
        manager.disconnect(websocket, run_id)

# ===================== HELPER FUNCTIONS =====================
import math

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters"""
    R = 6371000  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = math.sin(delta_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c

async def detect_and_claim_loop(run: Dict[str, Any], user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Detect if current run trace forms a valid closed loop and create claim"""
    points = run.get("points", [])
    if len(points) < 10:
        return None
    
    # Convert points to coordinates
    coords = [(p["lon"], p["lat"]) for p in points]
    
    # Check if last point is close to any earlier point (loop closure)
    last_point = coords[-1]
    loop_start_idx = None
    
    for i in range(len(coords) - 10):
        dist = haversine_distance(
            coords[i][1], coords[i][0],
            last_point[1], last_point[0]
        )
        if dist <= MAX_CLOSE_DISTANCE_M:
            loop_start_idx = i
            break
    
    if loop_start_idx is None:
        return None
    
    # Extract loop coordinates
    loop_coords = coords[loop_start_idx:]
    loop_coords.append(loop_coords[0])  # Close the loop
    
    if len(loop_coords) < 4:
        return None
    
    try:
        # Create polygon
        polygon = Polygon(loop_coords)
        
        if not polygon.is_valid:
            polygon = polygon.buffer(0)  # Fix invalid polygons
        
        if not polygon.is_valid or polygon.is_empty:
            return None
        
        # Calculate area (approximate, using simple projection)
        # For accurate area, would use proper projection
        centroid = polygon.centroid
        area_m2 = polygon.area * 111320 * 111320 * math.cos(math.radians(centroid.y))
        
        if area_m2 < MIN_POLYGON_AREA_M2:
            return None
        
        # Check for overlaps with existing claims
        await handle_claim_conflicts(polygon, user["user_id"])
        
        # Create claim
        claim_id = f"claim_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        
        claim_doc = {
            "claim_id": claim_id,
            "owner_id": user["user_id"],
            "owner_name": user["name"],
            "geometry": {
                "type": "Polygon",
                "coordinates": [list(polygon.exterior.coords)]
            },
            "area_m2": area_m2,
            "center_lat": centroid.y,
            "center_lon": centroid.x,
            "created_at": now,
            "last_maintained_at": now,
            "source_run_id": run["run_id"]
        }
        
        await db.claims.insert_one(claim_doc)
        
        # Update run with claim
        await db.runs.update_one(
            {"run_id": run["run_id"]},
            {
                "$push": {"claims": claim_id},
                "$inc": {"area_claimed_m2": area_m2}
            }
        )
        
        return {
            "claim_id": claim_id,
            "area_m2": area_m2,
            "geometry": claim_doc["geometry"],
            "message": f"Claimed {area_m2:.1f} sq meters!"
        }
    
    except Exception as e:
        logger.error(f"Error creating claim: {e}")
        return None

async def handle_claim_conflicts(new_polygon: Polygon, user_id: str) -> None:
    """Handle overlapping claims - simplified version"""
    # Get potentially overlapping claims
    bounds = new_polygon.bounds  # (minx, miny, maxx, maxy)
    
    existing_claims = await db.claims.find(
        {
            "center_lon": {"$gte": bounds[0] - 0.01, "$lte": bounds[2] + 0.01},
            "center_lat": {"$gte": bounds[1] - 0.01, "$lte": bounds[3] + 0.01},
            "owner_id": {"$ne": user_id}
        },
        {"_id": 0}
    ).to_list(100)
    
    for claim in existing_claims:
        try:
            existing_polygon = shape(claim["geometry"])
            
            if new_polygon.intersects(existing_polygon):
                intersection = new_polygon.intersection(existing_polygon)
                overlap_ratio = intersection.area / existing_polygon.area
                
                # If significant overlap, reduce the existing claim
                if overlap_ratio > 0.3:
                    remaining = existing_polygon.difference(new_polygon)
                    if remaining.is_empty or remaining.area < MIN_POLYGON_AREA_M2 / (111320 * 111320):
                        # Remove the claim entirely
                        await db.claims.delete_one({"claim_id": claim["claim_id"]})
                    else:
                        # Update the claim with reduced area
                        centroid = remaining.centroid
                        new_area = remaining.area * 111320 * 111320 * math.cos(math.radians(centroid.y))
                        await db.claims.update_one(
                            {"claim_id": claim["claim_id"]},
                            {
                                "$set": {
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [list(cast(Polygon, remaining).exterior.coords)]
                                    },
                                    "area_m2": new_area,
                                    "center_lat": centroid.y,
                                    "center_lon": centroid.x
                                }
                            }
                        )
        except Exception as e:
            logger.error(f"Error handling conflict for claim {claim['claim_id']}: {e}")

async def update_streak(user_id: str) -> None:
    """Update user's running streak"""
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
            # Already ran today, no change
            return
        elif days_diff == 1:
            # Consecutive day, increase streak
            current_streak += 1
        else:
            # Streak broken
            current_streak = 1
    else:
        current_streak = 1
    
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"current_streak": current_streak}}
    )

async def check_badges(user_id: str) -> None:
    """Check and award badges for user"""
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
            await db.user_badges.insert_one({
                "user_id": user_id,
                "badge_id": badge["badge_id"],
                "earned_at": now
            })
    
    if new_badges:
        await db.users.update_one(
            {"user_id": user_id},
            {"$push": {"badges": {"$each": new_badges}}}
        )

# ===================== ROOT ENDPOINT =====================
@api_router.get("/")
async def root() -> Dict[str, str]:
    return {"message": "Intertia API", "version": "1.0.0"}

@api_router.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "healthy"}

# Include router and middleware
app.include_router(api_router)

# Robust CORS for both local and Vercel environments
# We allow credentials but specify the origins instead of using "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "https://intertia-ui.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app", # This allows any of your vercel deployments
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
