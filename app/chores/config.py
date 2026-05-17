import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    db_path: str
    photo_dir: str
    secret_key: str
    checkin_password: str
    supervisor_password: str
    weekly_goal: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=os.environ.get("CHORES_DB_PATH", "./data/chores.db"),
            photo_dir=os.environ.get("CHORES_PHOTO_DIR", "./data/photos"),
            secret_key=os.environ.get("CHORES_SECRET_KEY", "dev-insecure-key"),
            checkin_password=os.environ.get("CHORES_CHECKIN_PASSWORD", "didi"),
            supervisor_password=os.environ.get("CHORES_SUPERVISOR_PASSWORD", "jia"),
            weekly_goal=int(os.environ.get("CHORES_WEEKLY_GOAL", "300")),
        )
