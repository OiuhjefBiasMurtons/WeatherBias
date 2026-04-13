import logging
from functools import lru_cache

from supabase import Client, create_client

from weathersniper.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Singleton Supabase client."""
    logger.info("Initializing Supabase client")
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
