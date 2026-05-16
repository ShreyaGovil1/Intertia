from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import List, Optional, Dict, Any


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class GoogleAuth(BaseModel):
    credential: str


class ForgotPassword(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    new_password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    email: str
    name: str
    picture: Optional[str] = None
    total_distance_m: float = 0
    total_area_m2: float = 0
    total_runs: int = 0
    current_streak: int = 0
    badges: List[str] = []
    created_at: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class RunPointInput(BaseModel):
    timestamp: str
    lat: float
    lon: float
    accuracy_m: float
    speed_mps: Optional[float] = None
    heading: Optional[float] = None


class RunStart(BaseModel):
    run_type: str = "solo"
    group_id: Optional[str] = None


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_id: str
    user_id: str
    status: str
    run_type: str
    group_id: Optional[str] = None
    started_at: str
    ended_at: Optional[str] = None
    distance_m: float = 0
    area_claimed_m2: float = 0
    points_count: int = 0
    claims: List[str] = []


class ClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_id: str
    owner_id: str
    owner_name: str
    geometry: Dict[str, Any]
    area_m2: float
    created_at: str
    last_maintained_at: str
    decay_percent: float = 0


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: str
    name: str
    picture: Optional[str] = None
    value: float
    metric: str


class GroupSession(BaseModel):
    model_config = ConfigDict(extra="ignore")
    group_id: str
    name: str
    creator_id: str
    members: List[str]
    status: str
    created_at: str
    max_members: int = 10


class BadgeResponse(BaseModel):
    badge_id: str
    name: str
    description: str
    icon: str
    earned_at: Optional[str] = None


class SeasonResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    season_id: str
    name: str
    description: str
    start_date: str
    end_date: str
    prizes: List[Dict[str, Any]]
    leaderboard: List[LeaderboardEntry]
