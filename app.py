# ============================================================
# FIX: Ensure the current directory is in Python's path
# ============================================================
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# Original imports (unchanged)
# ============================================================
import logging
import sys

def _get_eventlet():
    class _FallbackEventlet:
        @staticmethod
        def sleep(seconds):
            import time as _time
            _time.sleep(seconds)

        @staticmethod
        def spawn(func, *args, **kwargs):
            import threading
            thread = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
            thread.start()
            return thread

    if sys.platform == 'win32':
        logging.warning("Windows platform detected; using threading fallback instead of eventlet.")
        return _FallbackEventlet()

    if sys.version_info >= (3, 14):
        logging.warning("Skipping eventlet on Python 3.14+ due to known incompatibility; using threading fallback.")
        return _FallbackEventlet()

    try:
        import eventlet
        try:
            eventlet.monkey_patch()
            return eventlet
        except Exception as e:
            logging.warning(f"Eventlet monkey_patch failed: {e}")
    except ImportError as e:
        logging.warning(f"Eventlet import failed: {e}")

    return _FallbackEventlet()

eventlet = _get_eventlet()

# Standard library imports
import re
import os
import sys
import json
import random
import threading
import time
import csv
import string
from io import StringIO
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import radians, cos, sin, sqrt, atan2, ceil

# Third-party imports
import requests
from requests.structures import CaseInsensitiveDict
import smtplib
from flask import render_template, request, jsonify, session, redirect, url_for, flash, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, emit
try:
    from flask_caching import Cache
except Exception:
    # Fallback shim when flask_caching isn't installed in the environment.
    class Cache:
        def __init__(self, app=None, config=None):
            pass
        def cached(self, timeout=0, key_prefix=None):
            def decorator(f):
                return f
            return decorator
        def memoize(self, timeout=0):
            def decorator(f):
                return f
            return decorator
from dotenv import load_dotenv
from rich.panel import Panel
import validators
from sqlalchemy.exc import SQLAlchemyError, OperationalError
from sqlalchemy import inspect, text, or_
from time import sleep
from urllib.parse import quote_plus, urlparse
from telebot import TeleBot, types
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired
from flask_wtf import FlaskForm
from functools import wraps, lru_cache
import traceback
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
try:
    from unidecode import unidecode as _unidecode
except Exception:
    def _unidecode(s):
        return s

try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None
RAPIDFUZZ_THRESHOLD = int(os.getenv('RAPIDFUZZ_THRESHOLD', '70'))

# Small curated transliteration map for high-value or commonly problematic cities
HIGH_VALUE_TRANSLITERATION_MAP = {
    'רחובות': 'Rehovot, IL',
    'rehovot': 'Rehovot, IL',
    'תל אביב': 'Tel Aviv, IL',
    'תל-אביב': 'Tel Aviv, IL',
    'תלאביב': 'Tel Aviv, IL',
    'תלאביב': 'Tel Aviv, IL',
    'מדריד': 'Madrid, ES',
    'לונדון': 'London, UK',
}
from collections import deque, defaultdict

load_dotenv()

# Local imports
from utils import (
    redis_client, get_redis_client, get_redis_metrics, console, enqueue_notification,
    can_start_simulation, register_simulation_start, register_simulation_stop,
    email_throttle_cache, email_digest_cache,
    get_cached_route_templates, sanitize_tracking_number, validate_email,
    validate_location, validate_webhook_url,
    cache_route_templates, get_bot, get_shipment_list,
    get_shipment_details, save_shipment, update_shipment, invalidate_cache, is_admin,
    DHL_CONFIG, config as bot_config, db, app as utils_app,
    Shipment as UtilsShipment, estimate_distance, generate_unique_id, search_shipments, get_recent_logs,
    should_send_email, spawn_simulation, add_socket_event, recent_socket_events, add_client_error, recent_client_errors,
    EMAIL_TEST_MODE, EMAIL_ENABLED, AUTO_EMAIL_ENABLED, EMAIL_THROTTLE_MINUTES
)

# Import the new simulation engine
from simulator_engine import SimulationRunner, RunnerHooks, Stage

# Initialize Flask app from utils.py
app = utils_app
Shipment = UtilsShipment
config = bot_config

# Email configuration
# Values are defined in utils.py and baked into app config where needed

def limiter_request_identifier():
    try:
        return request.endpoint or request.path or ""
    except Exception:
        try:
            return request.path or ""
        except Exception:
            return ""


@app.before_request
def _sync_limiter_enabled_state():
    """Disable Flask-Limiter during unit tests to avoid request proxy compatibility issues."""
    if 'limiter' in globals():
        limiter.enabled = not app.testing


# Core extensions
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=app.config['RATELIMIT_DEFAULTS'],
    storage_uri=app.config['RATELIMIT_STORAGE_URI'],
    request_identifier=limiter_request_identifier,
    auto_check=False,
    enabled=not app.testing
)

@app.before_request
def _apply_rate_limiting():
    if app.testing or not limiter.enabled:
        return None
    return limiter._check_request_limit()


@limiter.request_filter
def exempt_internal_endpoints():
    """Keep internal service and admin endpoints outside public request limits.
    Also exempt all requests when running unit tests to avoid Flask-Limiter compatibility issues
    with the test request proxy and blueprint lookup.
    """
    if app.testing:
        return True

    path = request.path or ''
    return (
        path.startswith('/admin/')
        or path in {'/admin', '/notify', '/telegram/webhook', '/health', '/debug', '/admin/debug'}
    )


async_mode = 'eventlet' if hasattr(eventlet, 'sleep') and eventlet.__class__.__name__ != '_FallbackEventlet' else 'threading'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode)

# Configure cache: prefer Redis if available, otherwise simple in-memory cache
cache_config = {'CACHE_TYPE': 'simple'}
redis_url = (
    app.config.get('REDIS_URL') or
    os.getenv('REDIS_URL') or
    getattr(config, 'redis_url', None) or
    app.config.get('RATELIMIT_STORAGE_URI')
)
if redis_url:
    cache_config = {
        'CACHE_TYPE': 'RedisCache',
        'CACHE_REDIS_URL': redis_url
    }
cache = Cache(app, config=cache_config)

# Logging
flask_logger = logging.getLogger('flask_app')
sim_logger = logging.getLogger('simulator')

# Caches
geocode_cache = {}
in_memory_clients = {}
in_memory_sim = {}
# Per-tracking-number last lightweight broadcast timestamp
sim_last_broadcast = {}

# ============================================================
# GEOCODING WITH FALLBACK - Rate Limiting & Multi-Provider
# ============================================================

# Rate limiting for geocoding APIs
geocode_rate_limiter = defaultdict(list)

def rate_limit_geocode(api_name, min_interval=1.0):
    """Rate limit geocoding API calls."""
    now = time.time()
    if api_name in geocode_rate_limiter:
        # Remove old timestamps (older than 60 seconds)
        geocode_rate_limiter[api_name] = [
            t for t in geocode_rate_limiter[api_name] 
            if now - t < 60
        ]
        if geocode_rate_limiter[api_name]:
            last_call = geocode_rate_limiter[api_name][-1]
            if now - last_call < min_interval:
                time.sleep(min_interval - (now - last_call))
    geocode_rate_limiter[api_name].append(time.time())

def geoapify_geocode_fallback(address):
    """Geocode using Geoapify API (Primary)."""
    api_key = (
        app.config.get('GEOAPIFY_API_KEY') or 
        app.config.get('GEOCODING_API_KEY') or 
        os.getenv('GEOAPIFY_API_KEY') or 
        os.getenv('GEOCODING_API_KEY')
    )
    
    if not api_key:
        flask_logger.debug("No Geoapify API key configured")
        return None
    
    # Rate limit Geoapify calls (free tier: 1 request/second)
    rate_limit_geocode('geoapify', min_interval=1.0)
    
    start = time.time()
    try:
        url = f"https://api.geoapify.com/v1/geocode/search"
        params = {
            'text': address,
            'apiKey': api_key,
            'limit': 1,
            'format': 'json'
        }

        resp = requests.get(url, params=params, timeout=10)

        if resp.status_code == 429:
            flask_logger.warning(f"Geoapify rate limit exceeded for {address}")
            track_geocode_metrics('geoapify', address, False, time.time() - start)
            return None

        if resp.status_code != 200:
            flask_logger.debug(f"Geoapify returned {resp.status_code} for {address}")
            track_geocode_metrics('geoapify', address, False, time.time() - start)
            return None

        payload = resp.json()
        features = payload.get('features') or []
        if not features:
            track_geocode_metrics('geoapify', address, False, time.time() - start)
            return None

        props = features[0].get('properties', {})

        # Extract location info
        city = props.get('city') or props.get('town') or props.get('village') or props.get('county')
        country = props.get('country')
        country_code = props.get('country_code', '').upper()

        # Build formatted name
        if city and country_code:
            formatted = f"{city}, {country_code}"
        elif city and country:
            formatted = f"{city}, {country}"
        else:
            formatted = props.get('formatted') or props.get('address_line1') or address

        result = {
            'lat': float(props.get('lat', 0)),
            'lon': float(props.get('lon', 0)),
            'desc': address,
            'formatted': formatted,
            'city': city,
            'country': country,
            'country_code': country_code,
            'provider': 'geoapify'
        }
        track_geocode_metrics('geoapify', address, True, time.time() - start)
        return result
    except requests.exceptions.Timeout:
        flask_logger.warning(f"Geoapify timeout for {address}")
        track_geocode_metrics('geoapify', address, False, time.time() - start)
        return None
    except Exception as e:
        flask_logger.debug(f"Geoapify geocode failed for {address}: {e}")
        track_geocode_metrics('geoapify', address, False, time.time() - start)
        return None

def geocode_maps_co_fallback(address):
    """Geocode using Geocode.maps.co API (Fallback 1)."""
    api_key = (
        app.config.get('MAPS_CO_API_KEY') or 
        app.config.get('GEOCODING_API_KEY') or 
        os.getenv('MAPS_CO_API_KEY') or 
        os.getenv('GEOCODING_API_KEY')
    )
    
    # Rate limit maps.co calls (free tier: 1 request/second)
    rate_limit_geocode('maps_co', min_interval=1.0)
    
    try:
        # Build URL with or without API key
        if api_key:
            url = f"https://geocode.maps.co/search?q={quote_plus(address)}&api_key={api_key}"
        else:
            # Without API key, you get very limited requests
            url = f"https://geocode.maps.co/search?q={quote_plus(address)}"
        
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 429:
            flask_logger.warning(f"Geocode.maps.co rate limit exceeded for {address}")
            return None
            
        if resp.status_code != 200:
            flask_logger.debug(f"Geocode.maps.co returned {resp.status_code} for {address}")
            return None
            
        data = resp.json()
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
            
        item = data[0]
        
        # Extract location info
        display_name = item.get('display_name', address)
        parts = display_name.split(',') if display_name else [address]
        
        # Try to extract city and country
        city = parts[0].strip() if parts else address
        country = parts[-1].strip() if len(parts) > 1 else ''
        
        # Build formatted name
        if city and country:
            formatted = f"{city}, {country}"
        else:
            formatted = display_name or address
        
        return {
            'lat': float(item.get('lat', 0)),
            'lon': float(item.get('lon', 0)),
            'desc': address,
            'formatted': formatted,
            'city': city,
            'country': country,
            'provider': 'maps_co'
        }
    except requests.exceptions.Timeout:
        flask_logger.warning(f"Geocode.maps.co timeout for {address}")
        return None
    except Exception as e:
        flask_logger.debug(f"Geocode.maps.co failed for {address}: {e}")
        return None

def nominatim_geocode_fallback(address):
    """Geocode using OpenStreetMap Nominatim (Free, no API key, as last resort)."""
    try:
        # Nominatim requires a User-Agent and 1 second delay
        rate_limit_geocode('nominatim', min_interval=1.0)
        
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': address,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        headers = {
            'User-Agent': 'DHL-Tracking-System/2.0'
        }
        
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        
        if resp.status_code != 200:
            return None
            
        data = resp.json()
        if not data:
            return None
            
        item = data[0]
        
        # Extract address components
        address_details = item.get('address', {})
        city = (
            address_details.get('city') or 
            address_details.get('town') or 
            address_details.get('village') or 
            address_details.get('county')
        )
        country = address_details.get('country')
        country_code = address_details.get('country_code', '').upper()
        
        # Build formatted name
        if city and country_code:
            formatted = f"{city}, {country_code}"
        elif city and country:
            formatted = f"{city}, {country}"
        else:
            formatted = item.get('display_name', address)
        
        return {
            'lat': float(item.get('lat', 0)),
            'lon': float(item.get('lon', 0)),
            'desc': address,
            'formatted': formatted,
            'city': city,
            'country': country,
            'country_code': country_code,
            'provider': 'nominatim'
        }
    except Exception as e:
        flask_logger.debug(f"Nominatim geocode failed for {address}: {e}")
        return None

def resolve_from_known_locations_fallback(address):
    """Check if address is in our known locations database."""
    if not address:
        return None
    
    address = address.strip()
    normalized_input = address.rsplit(',', 1)[0].strip() + ', ' + address.rsplit(',', 1)[1].strip().upper() if ',' in address else address
    
    # Check exact matches
    for loc in [address, normalized_input]:
        if loc in KNOWN_LOCATION_COORDS:
            coords = KNOWN_LOCATION_COORDS[loc]
            return {
                'lat': float(coords['lat']),
                'lon': float(coords['lon']),
                'desc': address,
                'formatted': loc,
                'provider': 'known_locations'
            }
    
    # Check DHL hubs (use the class after it's defined)
    try:
        if address in DHLRealisticSimulator.DHL_HUBS:
            hub = DHLRealisticSimulator.DHL_HUBS[address]
            return {
                'lat': float(hub['lat']),
                'lon': float(hub['lon']),
                'desc': address,
                'formatted': address,
                'provider': 'dhl_hubs'
            }
    except NameError:
        pass
    
    # Fuzzy matching
    address_lower = address.lower()
    for known_loc in KNOWN_LOCATION_COORDS:
        if address_lower in known_loc.lower() or known_loc.lower().startswith(address_lower):
            coords = KNOWN_LOCATION_COORDS[known_loc]
            return {
                'lat': float(coords['lat']),
                'lon': float(coords['lon']),
                'desc': address,
                'formatted': known_loc,
                'provider': 'known_locations_fuzzy'
            }
    
    return None

def geocode_with_fallback(address):
    """
    Geocode an address with fallback chain:
    1. Geoapify (primary)
    2. Geocode.maps.co (fallback 1)
    3. OpenStreetMap Nominatim (fallback 2)
    4. Known locations database (final fallback)
    """
    if not address:
        return None
    
    # Check cache first
    cache_key = f"geocode_fallback:{address}"
    try:
        if redis_client and (cached := redis_client.get(cache_key)):
            return json.loads(cached)
    except Exception:
        pass
    
    # Try Geoapify (primary)
    flask_logger.debug(f"Attempting Geoapify geocode for: {address}")
    result = geoapify_geocode_fallback(address)
    if result:
        flask_logger.info(f"✅ Geoapify resolved: {address} -> {result.get('formatted')}")
        try:
            if redis_client:
                redis_client.setex(cache_key, 86400, json.dumps(result))
        except Exception:
            pass
        return result
    
    # Try Geocode.maps.co (fallback 1)
    flask_logger.debug(f"Geoapify failed, trying Geocode.maps.co for: {address}")
    result = geocode_maps_co_fallback(address)
    if result:
        flask_logger.info(f"✅ Geocode.maps.co resolved: {address} -> {result.get('formatted')}")
        try:
            if redis_client:
                redis_client.setex(cache_key, 86400, json.dumps(result))
        except Exception:
            pass
        return result
    
    # Try OpenStreetMap Nominatim (fallback 2)
    flask_logger.debug(f"Geocode.maps.co failed, trying Nominatim for: {address}")
    result = nominatim_geocode_fallback(address)
    if result:
        flask_logger.info(f"✅ Nominatim resolved: {address} -> {result.get('formatted')}")
        try:
            if redis_client:
                redis_client.setex(cache_key, 86400, json.dumps(result))
        except Exception:
            pass
        return result
    
    # All APIs failed, try known locations database
    flask_logger.debug(f"All APIs failed, trying known locations for: {address}")
    result = resolve_from_known_locations_fallback(address)
    if result:
        flask_logger.info(f"✅ Known locations resolved: {address} -> {result.get('formatted')}")
        try:
            if redis_client:
                redis_client.setex(cache_key, 86400, json.dumps(result))
        except Exception:
            pass
        return result
    
    flask_logger.warning(f"❌ All geocoding attempts failed for: {address}")
    return None

# ============================================================
# END GEOCODING WITH FALLBACK
# ============================================================

# --- Routing fallback using Geoapify (optional separate routing key) ---
def geoapify_route_with_fallback(coords, mode='drive'):
    """Enhanced routing with fallback."""
    api_key = (
        app.config.get('GEOAPIFY_ROUTING_KEY') or 
        app.config.get('GEOAPIFY_API_KEY') or 
        app.config.get('GEOCODING_API_KEY')
    )
    if not api_key or len(coords) < 2:
        return coords
    
    try:
        # Rate limit routing calls
        rate_limit_geocode('geoapify_routing', min_interval=1.0)
        
        waypoints = '|'.join(f"{c['lat']},{c['lon']}" for c in coords)
        url = f"https://api.geoapify.com/v1/routing"
        params = {
            'waypoints': waypoints,
            'mode': mode,
            'apiKey': api_key
        }
        resp = requests.get(url, params=params, timeout=10)
        
        if resp.status_code != 200:
            return coords
            
        payload = resp.json()
        features = payload.get('features') or []
        if not features:
            return coords
            
        geometry = features[0].get('geometry', {})
        if geometry.get('type') != 'LineString':
            return coords
            
        return [
            {'lat': float(lat), 'lon': float(lon)}
            for idx, (lon, lat) in enumerate(geometry.get('coordinates', []))
        ]
    except Exception as e:
        flask_logger.debug(f"Geoapify routing failed: {e}")
        return coords

# --- Metrics for geocoding ---
def track_geocode_metrics(provider, address, success, duration):
    """Track geocoding performance metrics."""
    try:
        if redis_client:
            key = f"geocode_metrics:{provider}"
            redis_client.lpush(key, json.dumps({
                'timestamp': datetime.now().isoformat(),
                'address': (address or '')[:50],
                'success': bool(success),
                'duration_ms': round(duration * 1000, 2)
            }))
            redis_client.ltrim(key, 0, 1000)
    except Exception:
        pass

# --- Cached geocode wrapper ---
@cache.memoize(timeout=86400)
def cached_geocode_with_fallback(address):
    return geocode_with_fallback(address)

# Minimum seconds between lightweight simulator broadcasts per tracking number
SIM_BROADCAST_INTERVAL_SEC = float(os.getenv('SIM_BROADCAST_INTERVAL_SEC', '2.0') or '2.0')

def sim_emit_light(tn, progress=None, current_location=None, current_lat=None, current_lon=None,
                   status=None, delivery_location=None, last_updated=None,
                   service_level=None, delivery_window=None, proof_of_delivery=None,
                   checkpoints=None, stage=None):
    """Emit a lightweight tracking_update containing only coords and progress, rate-limited per-TN.
    This avoids recomputing heavy route data in `broadcast_update`.
    """
    try:
        # Only emit if there are active clients viewing this tracking number
        try:
            clients = get_clients(tn) or set()
            if not clients:
                return
        except Exception:
            # If client lookup fails, be conservative and skip emitting
            return

        now = time.time()
        last = sim_last_broadcast.get(tn, 0)
        if now - last < SIM_BROADCAST_INTERVAL_SEC:
            return
        sim_last_broadcast[tn] = now
        payload = {'tracking_number': tn}
        if status is not None:
            payload['status'] = status
        if delivery_location is not None:
            payload['delivery_location'] = delivery_location
        if progress is not None:
            try:
                payload['progress'] = float(progress)
            except Exception:
                payload['progress'] = progress
        if current_location is not None:
            payload['current_location'] = current_location
        if current_lat is not None:
            try:
                payload['current_lat'] = float(current_lat)
            except Exception:
                payload['current_lat'] = current_lat
        if current_lon is not None:
            try:
                payload['current_lon'] = float(current_lon)
            except Exception:
                payload['current_lon'] = current_lon
        if checkpoints is not None:
            payload['checkpoints'] = checkpoints
        if last_updated is not None:
            payload['last_updated'] = last_updated
        if service_level is not None:
            payload['service_level'] = service_level
        if delivery_window is not None:
            payload['delivery_window'] = delivery_window
        if proof_of_delivery is not None:
            payload['proof_of_delivery'] = proof_of_delivery
        if stage is not None:
            payload['stage'] = stage
        try:
            socketio.emit('tracking_update', payload, namespace='/')
            sim_logger.debug(f"SIM_LIGHT_EMIT|{tn}|{payload}")
        except Exception:
            pass
    except Exception:
        pass


def rget(field, tn, default=None):
    """Safe redis.hget wrapper that returns decoded value or default when redis missing or errors."""
    global redis_client
    try:
        if not redis_client:
            return in_memory_sim.get(tn, {}).get(field, default)
        val = redis_client.hget(field, tn)
        if val is None:
            return in_memory_sim.get(tn, {}).get(field, default)
        if isinstance(val, bytes):
            return val.decode('utf-8')
        return val
    except Exception:
        redis_client = None
        return in_memory_sim.get(tn, {}).get(field, default)


def rset(field, tn, value):
    """Safe redis.hset wrapper that disables redis_client on failure."""
    global redis_client
    if not redis_client:
        try:
            in_memory_sim.setdefault(tn, {})[field] = value
        except Exception:
            pass
        return
    try:
        redis_client.hset(field, tn, value)
    except Exception:
        redis_client = None


def rkeys(pattern):
    try:
        if not redis_client:
            return []
        keys = redis_client.keys(pattern) or []
        return [k.decode() if isinstance(k, bytes) else k for k in keys]
    except Exception as e:
        flask_logger.warning(f"Redis keys failed for pattern {pattern}: {e}")
        return []


def rhgetall(key):
    try:
        if not redis_client:
            return {}
        d = redis_client.hgetall(key) or {}
        return { (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in d.items() }
    except Exception as e:
        flask_logger.warning(f"Redis hgetall failed for {key}: {e}")
        return {}


def rlist_lpop(key):
    try:
        if not redis_client:
            return None
        v = redis_client.lpop(key)
        if v is None:
            return None
        return v.decode() if isinstance(v, bytes) else v
    except Exception as e:
        flask_logger.warning(f"Redis lpop failed for {key}: {e}")
        return None


def rexists(key):
    try:
        if not redis_client:
            return False
        return bool(redis_client.exists(key))
    except Exception as e:
        flask_logger.warning(f"Redis exists check failed for {key}: {e}")
        return False


def rhlen(key):
    try:
        if not redis_client:
            return 0
        return redis_client.hlen(key)
    except Exception as e:
        flask_logger.warning(f"Redis hlen failed for {key}: {e}")
        return 0


def densify_route_coords(route_coords, max_segment_km=1.0):
    """Densify a route represented as a list of [lat, lon] or dicts with lat/lon.
    Inserts intermediate points so that no segment is longer than max_segment_km.
    """
    if not route_coords or len(route_coords) < 2:
        return route_coords or []
    pairs = []
    for p in route_coords:
        if isinstance(p, dict):
            pairs.append([float(p.get('lat')), float(p.get('lon'))])
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            pairs.append([float(p[0]), float(p[1])])
    out = [{'lat': pairs[0][0], 'lon': pairs[0][1]}]
    for a, b in zip(pairs, pairs[1:]):
        dist_km = haversine_distance(a[0], a[1], b[0], b[1])
        if dist_km <= 0:
            continue
        segments = max(1, int(ceil(dist_km / float(max_segment_km))))
        for i in range(1, segments + 1):
            frac = i / float(segments)
            lat = a[0] + (b[0] - a[0]) * frac
            lon = a[1] + (b[1] - a[1]) * frac
            out.append({'lat': lat, 'lon': lon})
    return out

@app.before_request
def log_request():
    request.start_time = time.time()
    flask_logger.debug(
        "Request start: %s %s from %s query=%s json=%s",
        request.method,
        request.path,
        request.remote_addr,
        request.args.to_dict(flat=False),
        request.get_json(silent=True)
    )


@app.before_request
def check_geocoding_config():
    """Check geocoding configuration at startup."""
    if not hasattr(app, '_geocoding_checked'):
        app._geocoding_checked = True
        
        geoapify_key = app.config.get('GEOAPIFY_API_KEY') or app.config.get('GEOCODING_API_KEY')
        maps_co_key = app.config.get('MAPS_CO_API_KEY') or app.config.get('GEOCODING_API_KEY')
        
        flask_logger.info("=" * 50)
        flask_logger.info("Geocoding Configuration:")
        flask_logger.info(f"  Geoapify API Key: {'✅ Configured' if geoapify_key else '❌ Not configured'}")
        flask_logger.info(f"  Geocode.maps.co API Key: {'✅ Configured' if maps_co_key else '❌ Not configured'}")
        flask_logger.info(f"  OpenStreetMap Nominatim: {'✅ Available (free, rate limited)'}")
        flask_logger.info(f"  Known Locations Database: {'✅ ' + str(len(KNOWN_LOCATION_COORDS)) + ' locations'}")
        flask_logger.info("=" * 50)
        
        # Test geocoding
        test_location = "Lagos, NG"
        name, coords = resolve_location(test_location)
        if coords:
            flask_logger.info(f"✅ Geocoding test successful: {test_location} -> {name} ({coords})")
        else:
            flask_logger.error(f"❌ Geocoding test FAILED for {test_location}")
            flask_logger.error("   Please check your API keys and network connectivity")

@app.after_request
def log_response(response):
    duration = (time.time() - getattr(request, 'start_time', time.time())) * 1000
    flask_logger.debug(
        "Request complete: %s %s status=%s duration=%.1fms",
        request.method,
        request.path,
        response.status,
        duration
    )
    return response

@app.errorhandler(Exception)
def handle_app_exception(error):
    flask_logger.exception("Unhandled exception during request %s %s", request.method, request.path)
    if app.debug:
        raise error
    return jsonify({"error": "Internal server error"}), 500

# Validate env
required = ['SECRET_KEY', 'SQLALCHEMY_DATABASE_URI']
for var in required:
    if not app.config.get(var):
        raise ValueError(f"Missing: {var}")

# Forms
class TrackForm(FlaskForm):
    tracking_number = StringField('Tracking Number', validators=[DataRequired()])
    email = StringField('Email (Optional)')
    submit = SubmitField('Track')

# DB Init
def init_db():
    with app.app_context():
        db.create_all()
        engine = db.engine
        if engine.dialect.name == 'sqlite':
            try:
                conn = engine.connect()
                existing = {row['name'] for row in conn.execute(text("PRAGMA table_info(shipments)")).mappings()}
                alterations = [
                    ("carrier", "ALTER TABLE shipments ADD COLUMN carrier VARCHAR(20) DEFAULT 'DHL';"),
                    ("origin_lat", "ALTER TABLE shipments ADD COLUMN origin_lat REAL;"),
                    ("origin_lon", "ALTER TABLE shipments ADD COLUMN origin_lon REAL;"),
                    ("delivery_lat", "ALTER TABLE shipments ADD COLUMN delivery_lat REAL;"),
                    ("delivery_lon", "ALTER TABLE shipments ADD COLUMN delivery_lon REAL;")
                ]
                for col, stmt in alterations:
                    if col not in existing:
                        try:
                            conn.execute(text(stmt))
                        except Exception as e:
                            flask_logger.warning(f"SQLite column add failed for {col}: {e}")
                conn.commit()
                flask_logger.info("DB initialized using SQLite")
            except Exception as e:
                flask_logger.warning(f"SQLite DB init failed: {e}")
            return

        max_retries = 5
        for attempt in range(max_retries):
            try:
                if inspectors := inspect(engine):
                    if 'shipments' not in inspectors.get_table_names():
                        db.create_all()
                    db.session.execute(text("""
                        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS carrier VARCHAR(20) DEFAULT 'DHL';
                    """))
                    db.session.execute(text("""
                        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS origin_lat REAL;
                    """))
                    db.session.execute(text("""
                        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS origin_lon REAL;
                    """))
                    db.session.execute(text("""
                        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivery_lat REAL;
                    """))
                    db.session.execute(text("""
                        ALTER TABLE shipments ADD COLUMN IF NOT EXISTS delivery_lon REAL;
                    """))
                    db.session.commit()
                    flask_logger.info("DB initialized")
                    return
                sleep(5 * (2 ** attempt))
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
    raise Exception("DB init failed")

# reCAPTCHA
def verify_recaptcha(token):
    if 'your-secret-key' in app.config['RECAPTCHA_SECRET_KEY']:
        return True
    try:
        r = requests.post(app.config['RECAPTCHA_VERIFY_URL'], data={
            'secret': app.config['RECAPTCHA_SECRET_KEY'],
            'response': token
        }, timeout=5)
        return r.json().get('success', False)
    except:
        return False

# Geocoding functions
def geoapify_geocode(address):
    api_key = app.config.get('GEOCODING_API_KEY', '')
    if not api_key:
        return None
    try:
        url = f"https://api.geoapify.com/v1/geocode/search?text={quote_plus(address)}&apiKey={api_key}"
        headers = CaseInsensitiveDict({"Accept": "application/json"})
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        features = payload.get('features') or []
        if not features:
            return None
        props = features[0].get('properties', {})
        return {'lat': float(props.get('lat')), 'lon': float(props.get('lon')), 'desc': address}
    except Exception as e:
        flask_logger.debug(f"Geoapify geocode failed for {address}: {e}")
        return None

def geoapify_route(coords, mode='drive'):
    api_key = app.config.get('GEOCODING_API_KEY', '')
    if not api_key or len(coords) < 2:
        return coords
    try:
        waypoints = '|'.join(f"{c['lat']},{c['lon']}" for c in coords)
        url = f"https://api.geoapify.com/v1/routing?waypoints={quote_plus(waypoints)}&mode={mode}&apiKey={api_key}"
        headers = CaseInsensitiveDict({"Accept": "application/json"})
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return coords
        payload = resp.json()
        features = payload.get('features') or []
        if not features:
            return coords
        geometry = features[0].get('geometry', {})
        if geometry.get('type') != 'LineString':
            return coords
        return [
            {'lat': float(lat), 'lon': float(lon), 'desc': f"Route point {idx+1}"}
            for idx, (lon, lat) in enumerate(geometry.get('coordinates', []))
        ]
    except Exception as e:
        flask_logger.debug(f"Geoapify routing failed: {e}")
        return coords

@lru_cache(maxsize=1000)
def cached_geocode(location):
    return geoapify_geocode(location)

def build_route_from_checkpoints(checkpoint_coords, mode='drive'):
    if len(checkpoint_coords) < 2:
        return checkpoint_coords
    # Prefer enhanced Geoapify routing with fallback; fall back to existing geoapify_route
    try:
        routed = geoapify_route_with_fallback(checkpoint_coords, mode=mode)
        if routed and len(routed) >= 2:
            return routed
    except Exception:
        pass
    return geoapify_route(checkpoint_coords, mode=mode)

def geocode_locations(checkpoints):
    coords = []
    api_key = app.config.get('GEOCODING_API_KEY')
    last_time = [0]
    for cp in checkpoints:
        if cp in geocode_cache:
            coords.append(geocode_cache[cp])
            continue
        loc = cp.split(' - ')[1] if ' - ' in cp else cp
        cache_key = f"geocode:{loc}"
        try:
            if time.time() - last_time[0] < 1:
                time.sleep(1 - (time.time() - last_time[0]))
            last_time[0] = time.time()
            if redis_client and (cached := redis_client.get(cache_key)):
                coord = json.loads(cached)
                geocode_cache[cp] = coord
                coords.append(coord)
                continue
            coord = cached_geocode(loc) if api_key else None
            if coord:
                coord['desc'] = cp
            if not coord:
                normalized_input = loc.rsplit(',', 1)[0].strip() + ', ' + loc.rsplit(',', 1)[1].strip().upper() if ',' in loc else loc
                fallback = KNOWN_LOCATION_COORDS.get(loc) or KNOWN_LOCATION_COORDS.get(normalized_input)
                if fallback:
                    coord = {
                        'lat': float(fallback['lat']),
                        'lon': float(fallback['lon']),
                        'desc': cp
                    }
            if coord:
                geocode_cache[cp] = coord
                if redis_client:
                    redis_client.set(cache_key, json.dumps(coord), ex=86400)
                coords.append(coord)
        except Exception:
            pass
    return coords


KNOWN_LOCATION_COORDS = {
    "Dublin, IE": {"lat": 53.349805, "lon": -6.26031},
    "Berlin, DE": {"lat": 52.5200, "lon": 13.4050},
    "Lagos, NG": {"lat": 6.5244, "lon": 3.3792},
    "Abuja, NG": {"lat": 9.0765, "lon": 7.3986},
    "Port Harcourt, NG": {"lat": 4.8156, "lon": 7.0498},
    "Taraba, NG": {"lat": 8.8932, "lon": 11.3596},
    "London, UK": {"lat": 51.5074, "lon": -0.1278},
    "Paris, FR": {"lat": 48.8566, "lon": 2.3522},
    "Madrid, ES": {"lat": 40.4168, "lon": -3.7038},
    "Rome, IT": {"lat": 41.9028, "lon": 12.4964},
    "Milan, IT": {"lat": 45.4642, "lon": 9.1900},
    "Amsterdam, NL": {"lat": 52.3676, "lon": 4.9041},
    "Brussels, BE": {"lat": 50.8503, "lon": 4.3517},
    "Lisbon, PT": {"lat": 38.7223, "lon": -9.1393},
    "Athens, GR": {"lat": 37.9838, "lon": 23.7275},
    "Stockholm, SE": {"lat": 59.3293, "lon": 18.0686},
    "Oslo, NO": {"lat": 59.9139, "lon": 10.7522},
    "Warsaw, PL": {"lat": 52.2297, "lon": 21.0122},
    "Prague, CZ": {"lat": 50.0755, "lon": 14.4378},
    "Vienna, AT": {"lat": 48.2082, "lon": 16.3738},
    "Zurich, CH": {"lat": 47.3769, "lon": 8.5417},
    "Cairo, EG": {"lat": 30.0444, "lon": 31.2357},
    "Johannesburg, ZA": {"lat": -26.2041, "lon": 28.0473},
    "Nairobi, KE": {"lat": -1.2921, "lon": 36.8219},
    "Dubai, UAE": {"lat": 25.2048, "lon": 55.2708},
    "Mumbai, IN": {"lat": 19.0760, "lon": 72.8777},
    "Delhi, IN": {"lat": 28.6139, "lon": 77.2090},
    "Singapore, SG": {"lat": 1.3521, "lon": 103.8198},
    "Hong Kong, HK": {"lat": 22.3193, "lon": 114.1694},
    "Tokyo, JP": {"lat": 35.6762, "lon": 139.6503},
    "Seoul, KR": {"lat": 37.5665, "lon": 126.9780},
    "Sydney, AU": {"lat": -33.8688, "lon": 151.2093},
    "New York, NY": {"lat": 40.7128, "lon": -74.0060},
    "Los Angeles, CA": {"lat": 34.0522, "lon": -118.2437},
    "Toronto, CA": {"lat": 43.6532, "lon": -79.3832},
    "Miami, FL": {"lat": 25.7617, "lon": -80.1918},
    "Sao Paulo, BR": {"lat": -23.5505, "lon": -46.6333},
    "Mexico City, MX": {"lat": 19.4326, "lon": -99.1332},
    "Jerusalem, IL": {"lat": 31.7683, "lon": 35.2137},
    "Tel Aviv, IL": {"lat": 32.0853, "lon": 34.7818},
    "Haifa, IL": {"lat": 32.7940, "lon": 34.9896},
    "Eilat, IL": {"lat": 29.5581, "lon": 34.9482},
    "Rehovot, IL": {"lat": 31.8928, "lon": 34.8111},
    "Rishon LeZion, IL": {"lat": 31.9730, "lon": 34.7925},
    "Petah Tikva, IL": {"lat": 32.0840, "lon": 34.8878},
    "Ashdod, IL": {"lat": 31.8014, "lon": 34.6435},
    "Ashkelon, IL": {"lat": 31.6688, "lon": 34.5743},
    "Beersheba, IL": {"lat": 31.2529, "lon": 34.7915},
    "Netanya, IL": {"lat": 32.3215, "lon": 34.8532},
    "Holon, IL": {"lat": 32.0158, "lon": 34.7874},
    "Bnei Brak, IL": {"lat": 32.0833, "lon": 34.8333},
    "Herzliya, IL": {"lat": 32.1624, "lon": 34.8447},
    "Kfar Saba, IL": {"lat": 32.1750, "lon": 34.9069},
    "Ra'anana, IL": {"lat": 32.1848, "lon": 34.8706},
    "Modiin, IL": {"lat": 31.8996, "lon": 35.0104},
    "Nazareth, IL": {"lat": 32.6996, "lon": 35.3035},
    "Tiberias, IL": {"lat": 32.7950, "lon": 35.5311},
    "Acre, IL": {"lat": 32.9272, "lon": 35.0763},
    "Nahariya, IL": {"lat": 33.0059, "lon": 35.0941},
    "Safed, IL": {"lat": 32.9646, "lon": 35.4960},
    "Kiryat Shmona, IL": {"lat": 33.2074, "lon": 35.5708},
    "Caesarea, IL": {"lat": 32.5010, "lon": 34.9020},
    "Kano, NG": {"lat": 12.0022, "lon": 8.5920},
    "Ibadan, NG": {"lat": 7.3775, "lon": 3.9470},
    "Accra, GH": {"lat": 5.6037, "lon": -0.1870},
    "Kumasi, GH": {"lat": 6.6885, "lon": -1.6244},
    "Dakar, SN": {"lat": 14.7167, "lon": -17.4677},
    "Addis Ababa, ET": {"lat": 9.0320, "lon": 38.7469},
    "Dar es Salaam, TZ": {"lat": -6.7924, "lon": 39.2083},
    "Kampala, UG": {"lat": 0.3476, "lon": 32.5825},
    "Casablanca, MA": {"lat": 33.5731, "lon": -7.5898},
    "Tunis, TN": {"lat": 36.8065, "lon": 10.1815},
    "Algiers, DZ": {"lat": 36.7538, "lon": 3.0588},
    "Moscow, RU": {"lat": 55.7558, "lon": 37.6173},
    "Istanbul, TR": {"lat": 41.0082, "lon": 28.9784},
    "Bucharest, RO": {"lat": 44.4268, "lon": 26.1025},
    "Budapest, HU": {"lat": 47.4979, "lon": 19.0402},
    "Athens, GR": {"lat": 37.9838, "lon": 23.7275},
    "Barcelona, ES": {"lat": 41.3874, "lon": 2.1686},
    "Manchester, UK": {"lat": 53.4808, "lon": -2.2426},
    "Zurich, CH": {"lat": 47.3769, "lon": 8.5417},
    "Chicago, US": {"lat": 41.8781, "lon": -87.6298},
    "Houston, US": {"lat": 29.7604, "lon": -95.3698},
    "Atlanta, US": {"lat": 33.7490, "lon": -84.3880},
    "Washington, US": {"lat": 38.9072, "lon": -77.0369},
    "Vancouver, CA": {"lat": 49.2827, "lon": -123.1207},
    "Montreal, CA": {"lat": 45.5017, "lon": -73.5673},
    "Lima, PE": {"lat": -12.0464, "lon": -77.0428},
    "Buenos Aires, AR": {"lat": -34.6037, "lon": -58.3816},
    "Santiago, CL": {"lat": -33.4489, "lon": -70.6693},
    "Rio de Janeiro, BR": {"lat": -22.9068, "lon": -43.1729},
    "Bangkok, TH": {"lat": 13.7563, "lon": 100.5018},
    "Kuala Lumpur, MY": {"lat": 3.1390, "lon": 101.6869},
    "Jakarta, ID": {"lat": -6.2088, "lon": 106.8456},
    "Manila, PH": {"lat": 14.5995, "lon": 120.9842},
    "Taipei, TW": {"lat": 25.0330, "lon": 121.5654},
    "Beijing, CN": {"lat": 39.9042, "lon": 116.4074},
    "Shanghai, CN": {"lat": 31.2304, "lon": 121.4737},
    "Perth, AU": {"lat": -31.9505, "lon": 115.8605},
    "Auckland, NZ": {"lat": -36.8509, "lon": 174.7645},
    "Benin City, NG": {"lat": 6.3350, "lon": 5.6037},
    "Enugu, NG": {"lat": 6.4584, "lon": 7.5464},
    "Kaduna, NG": {"lat": 10.5105, "lon": 7.4165},
    "Jos, NG": {"lat": 9.8965, "lon": 8.8583},
    "Tamale, GH": {"lat": 9.4075, "lon": -0.8533},
    "Mombasa, KE": {"lat": -4.0435, "lon": 39.6682},
    "Kigali, RW": {"lat": -1.9441, "lon": 30.0619},
    "Harare, ZW": {"lat": -17.8252, "lon": 31.0335},
    "Lusaka, ZM": {"lat": -15.3875, "lon": 28.3228},
    "Maputo, MZ": {"lat": -25.9692, "lon": 32.5732},
    "Cape Town, ZA": {"lat": -33.9249, "lon": 18.4241},
    "Durban, ZA": {"lat": -29.8587, "lon": 31.0218},
    "Pretoria, ZA": {"lat": -25.7479, "lon": 28.2293},
    "Tripoli, LY": {"lat": 32.8872, "lon": 13.1913},
    "Khartoum, SD": {"lat": 15.5007, "lon": 32.5599},
    "Kiev, UA": {"lat": 50.4501, "lon": 30.5234},
    "Sofia, BG": {"lat": 42.6977, "lon": 23.3219},
    "Belgrade, RS": {"lat": 44.7866, "lon": 20.4489},
    "Zagreb, HR": {"lat": 45.8150, "lon": 15.9819},
    "Ljubljana, SI": {"lat": 46.0569, "lon": 14.5058},
    "Bratislava, SK": {"lat": 48.1486, "lon": 17.1077},
    "Tallinn, EE": {"lat": 59.4370, "lon": 24.7536},
    "Riga, LV": {"lat": 56.9496, "lon": 24.1052},
    "Vilnius, LT": {"lat": 54.6872, "lon": 25.2797},
    "Reykjavik, IS": {"lat": 64.1466, "lon": -21.9426},
    "Edinburgh, UK": {"lat": 55.9533, "lon": -3.1883},
    "Birmingham, UK": {"lat": 52.4862, "lon": -1.8904},
    "Hamburg, DE": {"lat": 53.5511, "lon": 9.9937},
    "Munich, DE": {"lat": 48.1351, "lon": 11.5820},
    "Cologne, DE": {"lat": 50.9375, "lon": 6.9603},
    "Stuttgart, DE": {"lat": 48.7758, "lon": 9.1829},
    "Rotterdam, NL": {"lat": 51.9244, "lon": 4.4777},
    "Geneva, CH": {"lat": 46.2044, "lon": 6.1432},
    "Porto, PT": {"lat": 41.1579, "lon": -8.6291},
    "Valencia, ES": {"lat": 39.4699, "lon": -0.3763},
    "Seville, ES": {"lat": 37.3891, "lon": -5.9845},
    "Naples, IT": {"lat": 40.8518, "lon": 14.2681},
    "Turin, IT": {"lat": 45.0703, "lon": 7.6869},
    "Lyon, FR": {"lat": 45.7640, "lon": 4.8357},
    "Marseille, FR": {"lat": 43.2965, "lon": 5.3698},
    "Copenhagen, DK": {"lat": 55.6761, "lon": 12.5683},
    "Riyadh, SA": {"lat": 24.7136, "lon": 46.6753},
    "Jeddah, SA": {"lat": 21.4858, "lon": 39.1925},
    "Doha, QA": {"lat": 25.2854, "lon": 51.5310},
    "Kuwait City, KW": {"lat": 29.3759, "lon": 47.9774},
    "Muscat, OM": {"lat": 23.5880, "lon": 58.3829},
    "Manama, BH": {"lat": 26.2235, "lon": 50.5876},
    "Amman, JO": {"lat": 31.9454, "lon": 35.9284},
    "Beirut, LB": {"lat": 33.8938, "lon": 35.5018},
    "Baghdad, IQ": {"lat": 33.3152, "lon": 44.3661},
    "Karachi, PK": {"lat": 24.8607, "lon": 67.0011},
    "Lahore, PK": {"lat": 31.5204, "lon": 74.3587},
    "Islamabad, PK": {"lat": 33.6844, "lon": 73.0479},
    "Dhaka, BD": {"lat": 23.8103, "lon": 90.4125},
    "Colombo, LK": {"lat": 6.9271, "lon": 79.8612},
    "Kathmandu, NP": {"lat": 27.7172, "lon": 85.3240},
    "Hanoi, VN": {"lat": 21.0278, "lon": 105.8342},
    "Ho Chi Minh City, VN": {"lat": 10.8231, "lon": 106.6297},
    "Phnom Penh, KH": {"lat": 11.5564, "lon": 104.9282},
    "Yangon, MM": {"lat": 16.8409, "lon": 96.1735},
    "Osaka, JP": {"lat": 34.6937, "lon": 135.5023},
    "Kyoto, JP": {"lat": 35.0116, "lon": 135.7681},
    "Busan, KR": {"lat": 35.1796, "lon": 129.0756},
    "Guangzhou, CN": {"lat": 23.1291, "lon": 113.2644},
    "Shenzhen, CN": {"lat": 22.5431, "lon": 114.0579},
    "Ulaanbaatar, MN": {"lat": 47.8864, "lon": 106.9057},
    "Boston, US": {"lat": 42.3601, "lon": -71.0589},
    "San Francisco, US": {"lat": 37.7749, "lon": -122.4194},
    "Seattle, US": {"lat": 47.6062, "lon": -122.3321},
    "Dallas, US": {"lat": 32.7767, "lon": -96.7970},
    "Denver, US": {"lat": 39.7392, "lon": -104.9903},
    "Phoenix, US": {"lat": 33.4484, "lon": -112.0740},
    "Philadelphia, US": {"lat": 39.9526, "lon": -75.1652},
    "Detroit, US": {"lat": 42.3314, "lon": -83.0458},
    "Calgary, CA": {"lat": 51.0447, "lon": -114.0719},
    "Ottawa, CA": {"lat": 45.4215, "lon": -75.6972},
    "Panama City, PA": {"lat": 8.9824, "lon": -79.5199},
    "Bogota, CO": {"lat": 4.7110, "lon": -74.0721},
    "Quito, EC": {"lat": -0.1807, "lon": -78.4678},
    "Montevideo, UY": {"lat": -34.9011, "lon": -56.1645},
    "Asuncion, PY": {"lat": -25.2637, "lon": -57.5759},
    "Caracas, VE": {"lat": 10.4806, "lon": -66.9036},
    "Christchurch, NZ": {"lat": -43.5321, "lon": 172.6362},
    "Brisbane, AU": {"lat": -27.4698, "lon": 153.0251},
    "Melbourne, AU": {"lat": -37.8136, "lon": 144.9631},
    "Adelaide, AU": {"lat": -34.9285, "lon": 138.6007},
    "Hadera, IL": {"lat": 32.4340, "lon": 34.9196},
    "Karmiel, IL": {"lat": 32.9199, "lon": 35.2972},
    "Yavne, IL": {"lat": 31.8781, "lon": 34.7398},
    "Lod, IL": {"lat": 31.9514, "lon": 34.8953},
    "Ramla, IL": {"lat": 31.9292, "lon": 34.8656},
    "Sderot, IL": {"lat": 31.5250, "lon": 34.5969},
    "Dimona, IL": {"lat": 31.0686, "lon": 35.0331},
    "Arad, IL": {"lat": 31.2588, "lon": 35.2128},
    "Maale Adumim, IL": {"lat": 31.7770, "lon": 35.2980},
    "Beit Shemesh, IL": {"lat": 31.7456, "lon": 34.9867},
    "Nagoya, JP": {"lat": 35.1815, "lon": 136.9066},
    "Fukuoka, JP": {"lat": 33.5904, "lon": 130.4017},
    "Chongqing, CN": {"lat": 29.5630, "lon": 106.5516},
    "Chennai, IN": {"lat": 13.0827, "lon": 80.2707},
    "Bengaluru, IN": {"lat": 12.9716, "lon": 77.5946},
    "Kolkata, IN": {"lat": 22.5726, "lon": 88.3639},
    "Tehran, IR": {"lat": 35.6892, "lon": 51.3890},
    "Baku, AZ": {"lat": 40.4093, "lon": 49.8671},
    "Tbilisi, GE": {"lat": 41.7151, "lon": 44.8271},
    "Yerevan, AM": {"lat": 40.1872, "lon": 44.5152}
}

# Auto-populate `HIGH_VALUE_TRANSLITERATION_MAP` from `KNOWN_LOCATION_COORDS` to
# provide transliterated and tokenized lookup keys for common cities. Do not
# override any explicitly curated entries already present in the map.
try:
    import re as _re
    for _known in list(KNOWN_LOCATION_COORDS.keys()):
        try:
            canonical = _known  # e.g. 'Rehovot, IL'
            # City part (before comma) and full known key
            city_part = _known.split(',', 1)[0].strip()
            full_lower = _known.lower()
            city_lower = city_part.lower()
            # transliterated variants
            try:
                city_unidecode = _unidecode(city_part).lower()
                full_unidecode = _unidecode(_known).lower()
            except Exception:
                city_unidecode = city_lower
                full_unidecode = full_lower

            # cleaned versions (remove punctuation, normalize spaces)
            cleaned_city = _re.sub(r'[^a-z0-9 ]', ' ', city_unidecode).strip()
            cleaned_full = _re.sub(r'[^a-z0-9 ,]', ' ', full_unidecode).strip()

            variants = set([full_lower, full_unidecode, city_lower, city_unidecode, cleaned_city, cleaned_full])
            # add individual tokens from the city name (e.g., 'rishon', 'lezion')
            for tok in cleaned_city.split():
                if tok:
                    variants.add(tok)

            for key in variants:
                if not key:
                    continue
                # don't override hand-curated map entries
                if key in HIGH_VALUE_TRANSLITERATION_MAP:
                    continue
                HIGH_VALUE_TRANSLITERATION_MAP[key] = canonical
        except Exception:
            continue
except Exception:
    pass


def normalize_location(loc):
    """Normalize a free-text location into a readable 'City, CC' or fallback to a cleaned string.
    Uses Geoapify when API key is available and caches results in Redis when possible.
    Returns the normalized string.
    """
    if not loc:
        return loc
    loc = loc.strip()
    cache_key = f"normloc:{loc}"
    try:
        if redis_client and (cached := redis_client.get(cache_key)):
            return cached.decode('utf-8')
    except Exception:
        pass

    normalized = loc
    api_key = app.config.get('GEOCODING_API_KEY', '')
    try:
        if api_key:
            url = f"https://api.geoapify.com/v1/geocode/search?text={quote_plus(loc)}&apiKey={api_key}"
            resp = requests.get(url, timeout=6)
            if resp.status_code == 200:
                payload = resp.json()
                features = payload.get('features') or []
                if features:
                    props = features[0].get('properties', {})
                    city = props.get('city') or props.get('town') or props.get('village') or props.get('county')
                    country_code = props.get('country_code')
                    display = props.get('formatted') or props.get('address_line1') or props.get('name') or props.get('display_name')
                    if city and country_code:
                        normalized = f"{city}, {country_code.upper()}"
                    elif display:
                        normalized = display
        else:
            url = f" /search?q={quote_plus(loc)}"
            res = requests.get(url, timeout=5).json()
            if res:
                item = res[0]
                display = item.get('display_name')
                if display:
                    parts = [p.strip() for p in display.split(',')]
                    normalized = f"{parts[0]}, {parts[1]}" if len(parts) >= 2 else display
    except Exception:
        normalized = loc

    if normalized in KNOWN_LOCATION_COORDS:
        normalized = normalized

    try:
        if redis_client:
            redis_client.set(cache_key, normalized, ex=86400)
    except Exception:
        pass

    return normalized


def resolve_location(loc):
    """Enhanced resolve_location with retry logic."""
    if not loc:
        return loc, None

    loc = loc.strip()
    result = geocode_with_fallback_retry(loc)

    if result:
        name = result.get('formatted', loc)
        coords = {'lat': float(result['lat']), 'lon': float(result['lon'])}
        return name, coords

    return loc, None


def geocode_with_fallback_retry(address, max_retries=2):
    """Geocode with retry logic for transient failures."""
    last_error = None
    for attempt in range(max_retries):
        try:
            result = geocode_with_fallback(address)
            if result:
                return result
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                flask_logger.warning(f"Geocoding failed after {max_retries} attempts for {address}: {e}")
    return None

# WebSocket clients
def add_client(tn, sid):
    try:
        if redis_client:
            redis_client.sadd(f"clients:{tn}", sid)
        else:
            in_memory_clients.setdefault(tn, set()).add(sid)
    except Exception as e:
        flask_logger.warning(f"Redis add_client failed: {e}")
        try:
            in_memory_clients.setdefault(tn, set()).add(sid)
        except Exception:
            pass

def remove_client(tn, sid):
    try:
        if redis_client:
            redis_client.srem(f"clients:{tn}", sid)
        else:
            in_memory_clients.get(tn, set()).discard(sid)
    except Exception as e:
        flask_logger.warning(f"Redis remove_client failed: {e}")
        try:
            in_memory_clients.get(tn, set()).discard(sid)
        except Exception:
            pass

def get_clients(tn):
    try:
        if redis_client:
            return redis_client.smembers(f"clients:{tn}") or set()
        return in_memory_clients.get(tn, set())
    except Exception as e:
        flask_logger.warning(f"Redis get_clients failed: {e}")
        return in_memory_clients.get(tn, set())

# Background threads
def keep_alive():
    while True:
        try:
            requests.get(f"{app.config['WEBSOCKET_SERVER']}/health", timeout=10)
        except:
            pass
        eventlet.sleep(300)

def process_notification_queue():
    """
    NOTE: This function is no longer started automatically from
    start_background_services(). The dedicated `worker` service
    (worker.py, run as its own Render process) is now the sole
    consumer of the "notifications" Redis queue. Running this here
    AND worker.py at the same time causes both to race for the
    same queue items, which made email delivery inconsistent
    depending on which process happened to pop a given item first.

    The function is kept in place (unused) in case you ever want to
    run this app as a single combined process without a separate
    worker dyno -- in that case, call process_notification_queue()
    explicitly (e.g. via eventlet.spawn) and make sure worker.py is
    NOT also deployed.
    """
    while True:
        try:
            notif = rlist_lpop("notifications")
        except Exception as e:
            flask_logger.error(f"Notification pop failed: {e}")
            notif = None
        if not notif:
            eventlet.sleep(1)
            continue
        try:
            data = json.loads(notif)
            typ = data.get("type")
            d = data.get("data", {})
            if typ == "email":
                send_email_notification(
                    d.get("recipient_email"),
                    d.get("subject", "Shipment Update"),
                    d.get("html_body"),
                    d.get("plain_body")
                )
            elif typ == "webhook" and d.get("webhook_url"):
                try:
                    requests.post(d.get("webhook_url"), json={**d, "tracking_number": data.get("tracking_number")}, timeout=10)
                except Exception as e:
                    flask_logger.debug(f"Webhook notify failed in queue: {e}")
        except Exception as e:
            flask_logger.error(f"Queue error: {e}")

def cleanup_websocket_clients():
    while True:
        eventlet.sleep(3600)
        try:
            if redis_client:
                for key in redis_client.scan_iter("clients:*"):
                    try:
                        if isinstance(key, bytes):
                            tn = key.decode().split(":", 1)[1]
                        else:
                            tn = str(key).split(":", 1)[1]
                        for sid in redis_client.smembers(key):
                            try:
                                socketio.emit('ping', room=sid)
                            except Exception:
                                remove_client(tn, sid)
                    except Exception:
                        continue
        except Exception as e:
            flask_logger.warning(f"cleanup_websocket_clients failed: {e}")

# === REALISTIC DISTANCE FUNCTION ===
def haversine_distance(lat1, lon1, lat2, lon2):
    phi1 = radians(lat1)
    phi2 = radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return round(6371 * 2 * atan2(sqrt(a), sqrt(1 - a)), 1)


def estimate_distance(origin, dest):
    if not origin or not dest:
        return 1000

    origin_norm, origin_coords = resolve_location(origin)
    dest_norm, dest_coords = resolve_location(dest)
    if origin_coords and dest_coords:
        return haversine_distance(
            origin_coords['lat'], origin_coords['lon'],
            dest_coords['lat'], dest_coords['lon']
        )

    city_coords = {
        "Lagos, NG": (6.5244, 3.3792), "Abuja, NG": (9.0579, 7.4951), "Port Harcourt, NG": (4.8156, 7.0498),
        "Kano, NG": (12.0001, 8.5167), "Ibadan, NG": (7.3775, 3.9470), "Enugu, NG": (6.4584, 7.5170),
        "New York, NY": (40.7128, -74.0060), "Los Angeles, CA": (34.0522, -118.2437), "London, UK": (51.5074, -0.1278),
        "Dubai, UAE": (25.2048, 55.2708), "Tokyo, JP": (35.6762, 139.6503), "Sydney, AU": (-33.8688, 151.2093),
        "Paris, FR": (48.8566, 2.3522), "Berlin, DE": (52.5200, 13.4050), "Mumbai, IN": (19.0760, 72.8777),
        "Singapore, SG": (1.3521, 103.8198), "Hong Kong, HK": (22.3193, 114.1694), "São Paulo, BR": (-23.5505, -46.6333),
        "Johannesburg, ZA": (-26.2041, 28.0473), "Cairo, EG": (30.0444, 31.2357), "Moscow, RU": (55.7558, 37.6173),
        "Toronto, CA": (43.6532, -79.3832), "Mexico City, MX": (19.4326, -99.1332), "Seoul, KR": (37.5665, 126.9780),
        "Bangkok, TH": (13.7563, 100.5018), "Jakarta, ID": (-6.2088, 106.8456), "Delhi, IN": (28.7041, 77.1025),
        "Beijing, CN": (39.9042, 116.4074), "Shanghai, CN": (31.2304, 121.4737), "Istanbul, TR": (41.0082, 28.9784),
        "Karachi, PK": (24.8607, 67.0011), "Buenos Aires, AR": (-34.6037, -58.3816), "Rio de Janeiro, BR": (-22.9068, -43.1729),
        "Lima, PE": (-12.0464, -77.0428), "Bogotá, CO": (4.7110, -74.0721), "Santiago, CL": (-33.4489, -70.6693),
        "Cape Town, ZA": (-33.9249, 18.4241), "Nairobi, KE": (-1.2921, 36.8219), "Accra, GH": (5.6037, -0.1870),
        "Addis Ababa, ET": (8.9806, 38.7578), "Kuala Lumpur, MY": (3.1390, 101.6869), "Hanoi, VN": (21.0285, 105.8342),
        "Manila, PH": (14.5995, 120.9842), "Taipei, TW": (25.0330, 121.5654), "Riyadh, SA": (24.7136, 46.6753),
        "Tel Aviv, IL": (32.0853, 34.7818), "Athens, GR": (37.9838, 23.7275), "Lisbon, PT": (38.7223, -9.1393),
        "Stockholm, SE": (59.3293, 18.0686), "Oslo, NO": (59.9139, 10.7522), "Helsinki, FI": (60.1699, 24.9384),
        "Warsaw, PL": (52.2297, 21.0122), "Prague, CZ": (50.0755, 14.4378), "Budapest, HU": (47.4979, 19.0402),
        "Vienna, AT": (48.2082, 16.3738), "Zurich, CH": (47.3769, 8.5417), "Amsterdam, NL": (52.3676, 4.9041),
        "Brussels, BE": (50.8476, 4.3572), "Dublin, IE": (53.3498, -6.2603), "Madrid, ES": (40.4168, -3.7038),
        "Rome, IT": (41.9028, 12.4964), "Milan, IT": (45.4642, 9.1900), "Barcelona, ES": (41.3851, 2.1734)
    }
    origin_lower = origin.lower()
    dest_lower = dest.lower()
    origin_key = next((k for k in city_coords if origin_lower in k.lower() or k.lower().startswith(origin_lower)), None)
    dest_key = next((k for k in city_coords if dest_lower in k.lower() or k.lower().startswith(dest_lower)), None)
    if not origin_key or not dest_key:
        return 1000
    lat1, lon1 = city_coords[origin_key]
    lat2, lon2 = city_coords[dest_key]
    return haversine_distance(lat1, lon1, lat2, lon2)

# === DHL REALISTIC SIMULATOR (kept for hub building, service level, etc.) ===
class DHLRealisticSimulator:
    STATUS_CODES = {
        "Pending": "Shipment information received",
        "In_Transit": "In transit",
        "Out_for_Delivery": "Out for delivery",
        "Delivered": "Delivered",
        "Delayed": "Delayed",
        "Exception": "Exception - Contact DHL",
        "Customs_Clearance": "Customs clearance in progress",
        "Arrived_Destination": "Arrived at destination country",
        "Departed_Origin": "Departed origin facility"
    }

    REALISTIC_DELAYS = {
        "pickup": (30, 120),
        "sorting": (60, 240),
        "transit_local": (120, 600),
        "transit_international": (360, 2880),
        "customs": (180, 1440),
        "delivery": (60, 480),
        "delay_factor": (1.2, 3.0)
    }

    EVENT_MESSAGES = {
        "Pending": [
            "Shipment information received from shipper",
            "Electronic shipment data received",
            "Shipment details uploaded"
        ],
        "In_Transit": [
            "Processed at {location}",
            "Departed {location} facility",
            "Arrived at {location} sort facility",
            "Shipment in transit to {destination}",
            "Transferred through {location} hub",
            "In transit through {location}",
            "Processed through customs at {location}"
        ],
        "Out_for_Delivery": [
            "With delivery courier for final delivery",
            "Out for delivery from {location}",
            "Loaded onto delivery vehicle",
            "Scheduled for delivery today"
        ],
        "Delivered": [
            "Delivered successfully to recipient",
            "Signed by: {recipient}",
            "Delivered to {location} at {time}",
            "Proof of delivery available"
        ],
        "Delayed": [
            "Shipment delayed due to weather conditions at {location}",
            "Customs clearance delay at {location}",
            "Operational delay - rescheduled delivery",
            "Held for inspection at {location}",
            "Delay due to high shipment volume"
        ],
        "Exception": [
            "Shipment held - contact DHL for more information",
            "Delivery attempted - recipient not available",
            "Address issue - correction required",
            "Shipment damaged - inspection in progress"
        ]
    }

    SERVICE_LEVELS = {
        "DHL Express 9:00": {"premium": True, "delivery_window": "by 9:00 AM"},
        "DHL Express 12:00": {"premium": True, "delivery_window": "by 12:00 PM"},
        "DHL Express": {"premium": True, "delivery_window": "end of day"},
        "DHL Economy Select": {"premium": False, "delivery_window": "1-3 days"}
    }

    DHL_HUBS = {
        "Leipzig, DE": {"zone": "CET", "lat": 51.3397, "lon": 12.3731},
        "Hong Kong, HK": {"zone": "HKT", "lat": 22.3193, "lon": 114.1694},
        "Cincinnati, OH": {"zone": "EST", "lat": 39.1031, "lon": -84.5120},
        "Dubai, UAE": {"zone": "GST", "lat": 25.2048, "lon": 55.2708},
        "London, UK": {"zone": "GMT", "lat": 51.5074, "lon": -0.1278},
        "Frankfurt, DE": {"zone": "CET", "lat": 50.1109, "lon": 8.6821},
        "Singapore, SG": {"zone": "SGT", "lat": 1.3521, "lon": 103.8198},
        "Brussels, BE": {"zone": "CET", "lat": 50.8476, "lon": 4.3572},
        "Miami, FL": {"zone": "EST", "lat": 25.7617, "lon": -80.1918},
        "Tokyo, JP": {"zone": "JST", "lat": 35.6762, "lon": 139.6503},
        "Helsinki, FI": {"zone": "EET", "lat": 60.1699, "lon": 24.9384},
        "Stockholm, SE": {"zone": "CET", "lat": 59.3293, "lon": 18.0686},
        "Paris, FR": {"zone": "CET", "lat": 48.8566, "lon": 2.3522},
        "Madrid, ES": {"zone": "CET", "lat": 40.4168, "lon": -3.7038},
        "Berlin, DE": {"zone": "CET", "lat": 52.5200, "lon": 13.4050},
        "Milan, IT": {"zone": "CET", "lat": 45.4642, "lon": 9.1900},
        "Rome, IT": {"zone": "CET", "lat": 41.9028, "lon": 12.4964},
        "Warsaw, PL": {"zone": "CET", "lat": 52.2297, "lon": 21.0122},
        "Istanbul, TR": {"zone": "TRT", "lat": 41.0082, "lon": 28.9784},
        "Sao Paulo, BR": {"zone": "BRT", "lat": -23.5505, "lon": -46.6333},
        "Mexico City, MX": {"zone": "CST", "lat": 19.4326, "lon": -99.1332},
        "Johannesburg, ZA": {"zone": "SAST", "lat": -26.2041, "lon": 28.0473},
        "Nairobi, KE": {"zone": "EAT", "lat": -1.2921, "lon": 36.8219},
        "Vancouver, CA": {"zone": "PST", "lat": 49.2827, "lon": -123.1207},
        "Los Angeles, CA": {"zone": "PST", "lat": 34.0522, "lon": -118.2437},
        "Chicago, IL": {"zone": "CST", "lat": 41.8781, "lon": -87.6298},
        "Toronto, CA": {"zone": "EST", "lat": 43.6532, "lon": -79.3832},
        "Seoul, KR": {"zone": "KST", "lat": 37.5665, "lon": 126.9780},
        "Beijing, CN": {"zone": "CST", "lat": 39.9042, "lon": 116.4074},
        "Eilat, IL": {"zone": "IST", "lat": 29.5581, "lon": 34.9482},
        "Rehovot, IL": {"zone": "IST", "lat": 31.8928, "lon": 34.8111}
    }

    @staticmethod
    def is_business_hours(dt):
        return 9 <= dt.hour < 18 and dt.weekday() < 5

    @staticmethod
    def get_service_level(distance, is_business):
        if distance < 500 and is_business:
            return random.choices(
                ["DHL Express 9:00", "DHL Express 12:00", "DHL Express"],
                weights=[0.2, 0.3, 0.5]
            )[0]
        elif distance < 2000:
            return random.choices(
                ["DHL Express", "DHL Economy Select"],
                weights=[0.7, 0.3]
            )[0]
        return "DHL Express"

    @staticmethod
    def get_delivery_window(service_level, distance):
        now = datetime.now()
        if service_level == "DHL Express 9:00":
            delivery_date = now + timedelta(days=1)
            return f"{delivery_date.strftime('%B %d')} by 9:00 AM"
        elif service_level == "DHL Express 12:00":
            delivery_date = now + timedelta(days=1)
            return f"{delivery_date.strftime('%B %d')} by 12:00 PM"
        elif distance < 500:
            delivery_date = now + timedelta(days=1)
            return f"{delivery_date.strftime('%B %d')} (end of day)"
        elif distance < 2000:
            delivery_date = now + timedelta(days=2)
            return f"{delivery_date.strftime('%B %d')} (end of day)"
        else:
            delivery_date = now + timedelta(days=3)
            return f"{delivery_date.strftime('%B %d')} (end of day)"

    @staticmethod
    def generate_pod_info():
        names = ["J. SMITH", "M. JOHNSON", "R. WILLIAMS", "A. BROWN", "T. DAVIS"]
        signatures = [
            f"Signature: {random.choice(names)}",
            f"Signed by: {random.choice(['Front desk', 'Reception', 'Security', random.choice(names)])}",
            f"Delivery confirmation: {random.randint(1000, 9999)}"
        ]
        return random.choice(signatures)

    @staticmethod
    def get_closest_hubs(coords, count=2):
        if not coords:
            return []
        hubs = sorted(
            DHLRealisticSimulator.DHL_HUBS.items(),
            key=lambda item: haversine_distance(coords['lat'], coords['lon'], item[1]['lat'], item[1]['lon'])
        )
        return [name for name, _ in hubs[:count]]

    @staticmethod
    def build_route_hubs(origin_coords, dest_coords, distance_km):
        if not origin_coords or not dest_coords or distance_km < 800:
            return []
        origin_hubs = DHLRealisticSimulator.get_closest_hubs(origin_coords, count=1)
        dest_hubs = DHLRealisticSimulator.get_closest_hubs(dest_coords, count=1)
        if distance_km < 2000:
            return [hub for hub in origin_hubs + dest_hubs if hub not in origin_hubs or hub not in dest_hubs]
        middle_hub = "Frankfurt, DE" if abs(origin_coords['lon']) < 60 and abs(dest_coords['lon']) < 60 else "Dubai, UAE"
        hubs = []
        for hub in origin_hubs + [middle_hub] + dest_hubs:
            if hub and hub not in hubs:
                hubs.append(hub)
        return hubs

    @staticmethod
    def generate_realistic_checkpoint(city, status, tracking_number, **kwargs):
        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M")
        events = DHLRealisticSimulator.EVENT_MESSAGES.get(status, ["Shipment processed"])
        event_template = random.choice(events)
        event = event_template.format(
            location=city,
            destination=kwargs.get('destination', city),
            recipient=kwargs.get('recipient', 'CUSTOMER'),
            time=now.strftime("%I:%M %p")
        )
        facility_code = f"DHL{random.randint(100, 999)}"
        tracking_id = f"{facility_code}-{tracking_number[-4:]}"
        temp_info = ""
        if random.random() < 0.15:
            temp_info = f" | Temp-controlled: {random.randint(2, 8)}°C" if random.random() < 0.5 else ""
        return f"{time_str} - {city} - {event} [Ref: {tracking_id}]{temp_info}"

    @staticmethod
    def estimate_realistic_delivery_time(origin, destination):
        distance = estimate_distance(origin, destination)
        if distance <= 500:
            base_time = 24
            factor = 1
        elif distance <= 2000:
            base_time = 48
            factor = 1.5
        elif distance <= 8000:
            base_time = 72
            factor = 2
        else:
            base_time = 120
            factor = 3
        customs_delay = random.randint(12, 48) if distance > 1000 and random.random() < 0.3 else 0
        total_hours = base_time * factor + customs_delay
        return timedelta(hours=total_hours), total_hours / 9 * 1.4

    @staticmethod
    def generate_pickup_location(origin):
        pickup_locations = {
            "Lagos, NG": ["Ikeja Industrial Zone", "Apapa Port Complex", "Victoria Island Business District"],
            "Abuja, NG": ["Central Business District", "Garki Industrial Area", "Wuse Commercial Zone"],
            "Dubai, UAE": ["Jebel Ali Free Zone", "Dubai Airport Freezone", "Business Bay"],
            "London, UK": ["Heathrow Cargo Area", "Canary Wharf", "London City Business Park"],
            "New York, NY": ["JFK Cargo Area", "Times Square District", "Brooklyn Industrial Zone"],
            "Sydney, AU": ["Sydney Airport Cargo", "Parramatta Industrial", "CBD Business District"],
            "Rehovot, IL": ["Ha Yashim 12, Rehovot", "University Campus Area", "Kiryat Rehovot Industrial Zone"]
        }
        locations = pickup_locations.get(origin, ["Industrial Zone", "Business District"])
        return random.choice(locations)

    @staticmethod
    def generate_delivery_location(destination):
        delivery_locations = {
            "Lagos, NG": ["Adeola Odeku Street, VI", "Bourdillon Road, Ikoyi", "Awolowo Road, Ikoyi"],
            "Abuja, NG": ["Gana Street, Maitama", "Lagos Street, Garki", "Mambilla Street, Wuse"],
            "Dubai, UAE": ["Sheikh Zayed Road, Dubai Marina", "Jumeirah Beach Road", "Al Barsha District"],
            "London, UK": ["Brick Lane, Shoreditch", "King's Road, Chelsea", "Oxford Street, Mayfair"],
            "New York, NY": ["5th Avenue, Manhattan", "Wall Street, Financial District", "Broadway, Soho"],
            "Sydney, AU": ["George Street, CBD", "Bondi Beach Road", "Kings Cross, Potts Point"],
            "Rehovot, IL": ["Ha Yashim 12, Rehovot", "Kiryat Rehovot Main St", "Science Park, Rehovot"]
        }
        locations = delivery_locations.get(destination, ["Main Street", "City Center"])
        return random.choice(locations)

# ============================================================
# NEW SIMULATION ENGINE WRAPPER
# ============================================================

def _save_shipment_state(tn, status, checkpoints):
    """Update the shipment in the database and commit."""
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return
    shipment.status = status
    shipment.checkpoints = checkpoints
    shipment.last_updated = datetime.now()
    try:
        db.session.commit()
        invalidate_cache(tn)
    except Exception as e:
        flask_logger.error(f"Failed to save shipment state for {tn}: {e}")
        db.session.rollback()

def _handle_new_checkpoint(tn, checkpoint):
    """Handle a new checkpoint: log, trigger email if needed."""
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return
    try:
        checkpoints = (shipment.checkpoints or "").split(";")
        if len(checkpoints) > 1 and should_send_email(tn, shipment.status, checkpoints):
            enqueue_dhl_email(tn, shipment.status, checkpoint, shipment.delivery_location)
    except Exception as e:
        flask_logger.warning(f"Email handling failed for {tn}: {e}")

def simulate_tracking(tn):
    """Run the new leg-based simulation engine for a given tracking number."""
    with app.app_context():
        # Build the hooks object
        hooks = RunnerHooks(
            get_live_shipment=lambda: Shipment.query.filter_by(tracking_number=tn).first(),
            save_shipment=lambda status, checkpoints: _save_shipment_state(tn, status, checkpoints),
            get_flag=lambda field, default: rget(field, tn, default),
            set_flag=lambda field, value: rset(field, tn, value),
            resolve_location=resolve_location,
            build_route_hubs=DHLRealisticSimulator.build_route_hubs,
            on_position_update=lambda progress, city, lat, lon, stage: (
                # lightweight update for clients that only need coords/progress
                sim_emit_light(tn, progress=progress, current_location=city,
                               current_lat=lat, current_lon=lon, stage=stage)
            ),
            on_checkpoint_added=lambda checkpoint: _handle_new_checkpoint(tn, checkpoint),
            on_status_changed=lambda status: None,  # handled inside _commit
            broadcast=lambda: broadcast_update(tn),
            sleep=eventlet.sleep,
            now=datetime.now,
            generate_pod=DHLRealisticSimulator.generate_pod_info
        )

        sim_days = float(rget('sim_days', tn, os.getenv('SIM_DEFAULT_DAYS', '10')))
        runner = SimulationRunner(tn, hooks, sim_days_cap=sim_days)
        runner.run()

# ============================================================
# END NEW SIMULATION ENGINE
# ============================================================

# === DHL EMAIL ===
def build_dhl_email_html(tn, status, latest_checkpoint, destination, service_level=None, delivery_window=None):
    location = latest_checkpoint.split(' - ')[1] if ' - ' in latest_checkpoint else destination
    service_text = f"Service: {service_level or 'DHL Express'}"
    delivery_info = ""
    if status in ["In_Transit", "Out_for_Delivery", "Delayed"]:
        if delivery_window:
            delivery_info = f"<p style='margin: 0.3rem 0 0; color: #374151;'><strong>Estimated Delivery:</strong> {delivery_window}</p>"
        else:
            delivery_info = "<p style='margin: 0.3rem 0 0; color: #374151;'><strong>Estimated Delivery:</strong> Pending</p>"

    hold_info = ""
    if status in ["On_Hold"]:
        hold_info = """
        <div style="margin: 1rem 0; padding: 1rem 1.1rem; background: #fff7ed; border: 1px solid #fdba74; border-left: 4px solid #b45309; border-radius: 8px;">
          <p style="margin: 0 0 0.4rem; font-size: 1rem; font-weight: 700; color: #9a2c00;">Customs Clearance Hold</p>
          <p style="margin: 0; color: #7c2d12; line-height: 1.6;">
            Your shipment is currently on hold for customs clearance. Additional documentation or action may be required before it can continue.
            We will notify you as soon as the shipment is released.
          </p>
        </div>
        """

    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto; border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden; background-color: #f9fafb;">
      <div style="background: linear-gradient(90deg, #D40511 0%, #b2040f 100%); padding: 1.2rem; text-align: center;">
        <img src="{DHL_CONFIG['logo_url']}" alt="DHL" width="120" style="display: inline-block;">
      </div>
      <div style="padding: 1.5rem; background: #fff;">
        <h3 style="color: #D40511; margin: 0 0 0.8rem; font-size: 1.25rem;">Shipment Update</h3>
        <p style="margin: 0.3rem 0; color: #374151;"><strong>Waybill:</strong> <span style="background:#f3f4f6;padding:2px 6px;border-radius:4px;font-family:monospace;">{tn}</span></p>
        <p style="margin: 0.3rem 0; color: #374151;"><strong>Status:</strong> <span style="color:#D40511;font-weight:bold;">{status}</span></p>
        <p style="margin: 0.3rem 0; color: #374151;"><strong>Location:</strong> {location}</p>
        <p style="margin: 0.3rem 0; color: #374151;"><strong>Destination:</strong> {destination}</p>
        {delivery_info}
        {hold_info}
        <p style="margin: 1rem 0 0; color: #374151;"><strong>{service_text}</strong></p>
        <hr style="border:0;border-top:1px solid #e5e7eb;margin:1.25rem 0;">
        <div style="text-align: center;">
          <a href="{app.config['WEBSOCKET_SERVER']}/track/{tn}" style="background:#D40511;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;display:inline-block;font-weight:bold;">
            Track Shipment
          </a>
        </div>
        <p style="font-size:0.9rem;color:#6b7280;margin:1rem 0 0; text-align:center;">
          Need help? Contact DHL Express Support
        </p>
      </div>
      <div style="background:#FFCC00;padding:0.8rem;text-align:center;font-size:0.8rem;color:#111827;">
        © {datetime.now().year} DHL International GmbH. All rights reserved.
      </div>
    </div>
    """

def enqueue_dhl_email(tn, status, latest_checkpoint, destination, service_level=None, delivery_window=None):
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment or not shipment.recipient_email or not shipment.email_notifications:
        return
    distance_km = estimate_distance(shipment.origin_location or "Lagos, NG", destination)
    service_level = service_level or DHLRealisticSimulator.get_service_level(
        distance_km, DHLRealisticSimulator.is_business_hours(datetime.now())
    )
    if delivery_window is None and status in ["In_Transit", "Out_for_Delivery", "Delayed"]:
        delivery_window = DHLRealisticSimulator.get_delivery_window(service_level, distance_km)
    location = latest_checkpoint.split(' - ')[1] if ' - ' in latest_checkpoint else destination
    subject = f"DHL Shipment {tn} - {status}"
    html_body = build_dhl_email_html(tn, status, latest_checkpoint, destination, service_level, delivery_window)
    plain_body = f"DHL Update: {tn}\nStatus: {status}\nLocation: {location}\nService: {service_level}\nEstimated Delivery: {delivery_window or 'Pending'}\nTrack: {app.config['WEBSOCKET_SERVER']}/track/{tn}"
    enqueue_notification({
        "tracking_number": tn,
        "type": "email",
        "data": {
            "recipient_email": shipment.recipient_email,
            "subject": subject,
            "html_body": html_body,
            "plain_body": plain_body
        }
    })

# === EMAIL SENDER ===
def open_smtp_connection(timeout=None):
    """Open and authenticate an SMTP connection for STARTTLS or implicit SSL."""
    host = app.config['SMTP_HOST']
    port = int(app.config['SMTP_PORT'])
    timeout = timeout or int(os.getenv('SMTP_TIMEOUT', '120'))
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.starttls()
    server.login(app.config['SMTP_USER'], app.config['SMTP_PASS'])
    return server


def send_email_via_resend(recipient, subject, html_body=None, plain_body=None):
    api_key = app.config.get('RESEND_API_KEY', '')
    if not api_key:
        return False
    payload = {
        'from': app.config.get('SMTP_FROM', 'onboarding@resend.dev'),
        'to': [recipient],
        'subject': subject,
        'html': html_body or f'<p>{plain_body or ""}</p>'
    }
    if plain_body:
        payload['text'] = plain_body
    response = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=20
    )
    response.raise_for_status()
    return True


def send_email_notification(recipient, subject, html_body=None, plain_body=None, tracking_number=None, email_type=None, message=None):
    if EMAIL_TEST_MODE:
        flask_logger.info(f"📧 TEST MODE - Email would be sent to: {recipient}")
        flask_logger.info(f"   Subject: {subject}")
        flask_logger.info(f"   Tracking: {tracking_number}")
        return True
    if not EMAIL_ENABLED:
        flask_logger.info(f"📧 Email disabled - would send to {recipient}: {subject}")
        return True
    email_provider = 'resend' if app.config.get('RESEND_API_KEY') else app.config.get('EMAIL_PROVIDER', 'smtp')
    if not all([app.config['SMTP_HOST'], app.config['SMTP_USER'], app.config['SMTP_PASS']]):
        flask_logger.warning("SMTP not configured")
        if email_provider != 'resend':
            return False
    msg = MIMEMultipart("alternative")
    msg['From'] = app.config['SMTP_FROM']
    msg['To'] = recipient
    msg['Subject'] = subject
    if plain_body:
        msg.attach(MIMEText(plain_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))
    @retry(reraise=True, stop=stop_after_attempt(int(os.getenv('SMTP_MAX_RETRIES', '3'))), wait=wait_exponential(multiplier=2, max=30))
    def _send():
        if email_provider == 'resend':
            send_email_via_resend(recipient, subject, html_body, plain_body)
        else:
            with open_smtp_connection() as server:
                server.send_message(msg)

    try:
        _send()
        flask_logger.info(f"Email sent to {recipient}")
        if tracking_number:
            try:
                history_key = f"email_history:{tracking_number}"
                entry = {
                    'timestamp': datetime.now().isoformat(),
                    'type': email_type or 'status_update',
                    'recipient': recipient,
                    'subject': subject,
                    'message': message
                }
                if redis_client:
                    redis_client.lpush(history_key, json.dumps(entry))
                    redis_client.ltrim(history_key, 0, 99)
            except Exception as history_exc:
                flask_logger.warning('Failed to store email history for %s: %s', tracking_number, history_exc)
        return True
    except Exception as e:
        flask_logger.error(f"Email failed after retries: {e}")
        flask_logger.debug(traceback.format_exc())
        try:
            console.print(Panel(f"[error]Failed to send email to {recipient}[/error]", title="Email Error"))
        except Exception:
            pass
        return False

# === BROADCAST UPDATE (with fallback for route) ===
def broadcast_update(tn):
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return
    speed = float(rget("sim_speed_multipliers", tn, "1.0") or "1.0")
    paused = rget("paused_simulations", tn, "false") == "true"
    lock_stage = rget("lock_stage", tn, "false") == "true"
    try:
        coords = geocode_locations((shipment.checkpoints or "").split(";"))
        # --- FALLBACK: if checkpoint geocoding fails, use origin/destination ---
        if len(coords) < 2:
            origin_coords = None
            dest_coords = None
            if shipment.origin_lat is not None and shipment.origin_lon is not None:
                origin_coords = {
                    'lat': shipment.origin_lat,
                    'lon': shipment.origin_lon,
                    'desc': shipment.origin_location or 'Origin'
                }
            if shipment.delivery_lat is not None and shipment.delivery_lon is not None:
                dest_coords = {
                    'lat': shipment.delivery_lat,
                    'lon': shipment.delivery_lon,
                    'desc': shipment.delivery_location or 'Destination'
                }
            if origin_coords and dest_coords:
                coords = [origin_coords, dest_coords]
            elif origin_coords:
                coords = [origin_coords]
            elif dest_coords:
                coords = [dest_coords]
        # --- end fallback ---
        route_coords = build_route_from_checkpoints(coords, mode='drive')
        try:
            dens_km = float(os.getenv('SIM_ROUTE_DENSIFY_KM', '1.0') or '1.0')
            route_coords = densify_route_coords(route_coords, dens_km)
        except Exception:
            pass
    except Exception as e:
        flask_logger.warning(f"Geocoding failed for {tn}: {e}")
        coords = []
        route_coords = []
    progress = float(rget("progress", tn, "0") or "0")
    service_level = rget("service_level", tn, "DHL Express") or "DHL Express"
    delivery_window = rget("delivery_window", tn, "Calculating...") or "Calculating..."
    proof_of_delivery = rget("proof_of_delivery", tn, "Pending") or "Pending"
    current_location = rget('current_location', tn, '') or ''
    current_lat = rget('current_lat', tn, None)
    current_lon = rget('current_lon', tn, None)
    stage = rget('stage', tn, 'pickup') or 'pickup'

    # Compute current checkpoint index
    checkpoints = (shipment.checkpoints or "").split(";")
    current_checkpoint_index = 0
    if checkpoints:
        current_checkpoint_index = min(int(progress * len(checkpoints)), len(checkpoints) - 1)

    data = {
        "tracking_number": tn,
        "status": shipment.status,
        "delivery_location": shipment.delivery_location,
        "checkpoints": checkpoints,
        "coords": [{'lat': c['lat'], 'lon': c['lon'], 'desc': c['desc']} for c in coords],
        "route_coords": route_coords,
        "last_updated": shipment.last_updated.isoformat(),
        "progress": progress,
        "current_location": current_location,
        "current_lat": float(current_lat) if current_lat is not None else None,
        "current_lon": float(current_lon) if current_lon is not None else None,
        "service_level": service_level,
        "delivery_window": delivery_window,
        "proof_of_delivery": proof_of_delivery,
        "speed_multiplier": speed,
        "paused": paused,
        "carrier": shipment.carrier,
        "stage": stage,
        "lock_stage": lock_stage,
        "current_checkpoint_index": current_checkpoint_index
    }
    try:
        socketio.emit('tracking_update', data, namespace='/')
    except TypeError:
        try:
            socketio.emit('tracking_update', data, namespace='/')
        except Exception as e:
            flask_logger.warning(f"Socket emit failed for {tn}: {e}")

    websocket_server = app.config.get('WEBSOCKET_SERVER', '')
    try:
        parsed_ws = urlparse(websocket_server)
        if parsed_ws.scheme and parsed_ws.hostname and parsed_ws.hostname not in ('localhost', '127.0.0.1'):
            webhook_url = f"{websocket_server.rstrip('/')}/notify"
            requests.post(webhook_url, json=data, timeout=2)
    except Exception as e:
        flask_logger.debug(f"Webhook notify skipped for {tn}: {e}")

# Admin decorator
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# Admin diagnostics endpoints
@app.route('/admin/api/redis_metrics')
@admin_required
def admin_redis_metrics():
    try:
        metrics = get_redis_metrics()
        return jsonify(metrics)
    except Exception as e:
        flask_logger.error(f"Failed to fetch redis metrics: {e}")
        return jsonify({'error': 'Could not retrieve metrics'}), 500


@app.route('/admin/api/error_codes')
@admin_required
def admin_error_codes():
    """Expose mapping of `error_code` values to friendly messages for the admin UI."""
    mapping = {
        'missing_fields': 'Origin and destination are required',
        'invalid_service_level': 'Selected service level is not valid',
        'invalid_recipient_email': 'Recipient email is invalid',
        'geocoding_failed': 'Could not resolve the address. Try City, Country Code',
        'db_save_failed': 'Internal error saving shipment',
    }
    return jsonify(mapping)

@app.route('/admin/api/logs')
@admin_required
def admin_logs():
    try:
        lines = int(request.args.get('lines', 200))
    except Exception:
        lines = 200
    log_file = app.config.get('LOG_FILE', 'app.log')
    try:
        import os
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                data = f.readlines()[-lines:]
            return Response(''.join(data), mimetype='text/plain')
    except Exception:
        pass
    recent = get_recent_logs(min(lines, 50))
    return jsonify({'logs': recent})

@app.route('/admin/api/simulator_status')
@admin_required
def admin_simulator_status():
    try:
        from utils import SIMULATOR_THREAD_LIMIT, _simulator_thread_count, can_start_simulation
        return jsonify({
            'active_simulators': _simulator_thread_count,
            'limit': SIMULATOR_THREAD_LIMIT,
            'can_start': can_start_simulation(),
            'throttle_active': not can_start_simulation()
        })
    except Exception as e:
        flask_logger.error(f"Failed to fetch simulator status: {e}")
        return jsonify({'error': 'Could not retrieve simulator status'}), 500

# === FAVICON ROUTE ===
@app.route('/favicon.ico')
def favicon():
    try:
        return redirect('https://www.dhl.com/favicon.ico')
    except:
        return '', 204

# === PUBLIC ROUTES ===
@app.route('/')
def index():
    form = TrackForm()
    recaptcha_key = app.config.get('RECAPTCHA_SITE_KEY', '')
    host = request.host or ''
    if app.debug or app.config.get('FLASK_ENV') == 'development' or 'your-site-key' in (recaptcha_key or '') or 'localhost' in host or '127.0.0.1' in host:
        recaptcha_key = ''
    return render_template('index.html', form=form, tawk_property_id=app.config['TAWK_PROPERTY_ID'],
                           tawk_widget_id=app.config['TAWK_WIDGET_ID'], recaptcha_site_key=recaptcha_key)

def _render_tracking_response(rendered_html, status_code=200):
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'html': rendered_html}), status_code
    return rendered_html, status_code

@app.route('/track', methods=['POST'])
@limiter.limit("10 per minute")
def track():
    try:
        from forms import TrackForm as F
        form = F()
    except:
        form = TrackForm()
    if not form.validate_on_submit():
        if app.testing or request.form.get('submit') == 'Track' or request.form.get('tracking_number'):
            form.tracking_number.data = request.form.get('tracking_number', '')
            form.email.data = request.form.get('email', '')
        else:
            return _render_tracking_response(render_template('tracking_result.html', error='Invalid form submission', coords=[]), 400)
    
    
    tn = sanitize_tracking_number(form.tracking_number.data)
    email = form.email.data
    if not tn:
        return _render_tracking_response(render_template('tracking_result.html', error='Invalid tracking number', coords=[]), 400)
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return _render_tracking_response(render_template('tracking_result.html', error='Not found', coords=[]), 404)
    if email and validate_email(email):
        shipment.recipient_email = email
        db.session.commit()
        invalidate_cache(tn)
    # Start simulation (if not already) – this can remain here.
    if shipment.status not in ['Delivered', 'Returned']:
        try:
            if can_start_simulation():
                spawn_simulation(tn)
            else:
                flask_logger.info(f"Simulator throttle active; skipping new thread for {tn}")
        except Exception:
            if can_start_simulation():
                eventlet.spawn(simulate_tracking, tn)
            else:
                flask_logger.info(f"Simulator throttle active; skipping eventlet spawn for {tn}")

    # ✨ FIX: Redirect to the GET endpoint to make refreshes safe.
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # For AJAX requests, return the redirect URL.
        return jsonify({'redirect': url_for('track_direct', tracking_number=tn)})
    return redirect(url_for('track_direct', tracking_number=tn))

@app.route('/track/<tracking_number>')
def track_direct(tracking_number):
    tn = sanitize_tracking_number(tracking_number)
    if not tn:
        return render_template('tracking_result.html', error='Invalid tracking number', coords=[])
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return render_template('tracking_result.html', error='Not found', coords=[])
    checkpoints = (shipment.checkpoints or "").split(";")
    coords = geocode_locations(checkpoints)
    # --- FALLBACK: use origin/destination if checkpoints fail ---
    if len(coords) < 2:
        origin_coords = None
        dest_coords = None
        if shipment.origin_lat is not None and shipment.origin_lon is not None:
            origin_coords = {
                'lat': shipment.origin_lat,
                'lon': shipment.origin_lon,
                'desc': shipment.origin_location or 'Origin'
            }
        if shipment.delivery_lat is not None and shipment.delivery_lon is not None:
            dest_coords = {
                'lat': shipment.delivery_lat,
                'lon': shipment.delivery_lon,
                'desc': shipment.delivery_location or 'Destination'
            }
        if origin_coords and dest_coords:
            coords = [origin_coords, dest_coords]
        elif origin_coords:
            coords = [origin_coords]
        elif dest_coords:
            coords = [dest_coords]
    # --- end fallback ---
    coords_list = [{'lat': c['lat'], 'lon': c['lon'], 'desc': c['desc']} for c in coords]
    route_coords = build_route_from_checkpoints(coords_list, mode='drive')
    try:
        dens_km = float(os.getenv('SIM_ROUTE_DENSIFY_KM', '1.0') or '1.0')
        route_coords = densify_route_coords(route_coords, dens_km)
    except Exception:
        pass
    distance_km = estimate_distance(shipment.origin_location or "Lagos, NG", shipment.delivery_location)
    service_level = DHLRealisticSimulator.get_service_level(
        distance_km, DHLRealisticSimulator.is_business_hours(datetime.now())
    )
    delivery_window = DHLRealisticSimulator.get_delivery_window(service_level, distance_km)
    proof_of_delivery = DHLRealisticSimulator.generate_pod_info()
    if shipment.status not in ['Delivered', 'Returned']:
        try:
            if can_start_simulation():
                spawn_simulation(tn)
            else:
                flask_logger.info(f"Simulator throttle active; skipping new thread for {tn}")
        except Exception:
            if can_start_simulation():
                eventlet.spawn(simulate_tracking, tn)
            else:
                flask_logger.info(f"Simulator throttle active; skipping eventlet spawn for {tn}")
    progress = float(rget('progress', tn, '0') or '0')
    current_location = rget('current_location', tn, '') or ''
    current_lat = rget('current_lat', tn, None)
    current_lon = rget('current_lon', tn, None)
    stage = rget('stage', tn, 'pickup') or 'pickup'

    # Compute current checkpoint index
    current_checkpoint_index = 0
    if checkpoints:
        current_checkpoint_index = min(int(progress * len(checkpoints)), len(checkpoints) - 1)

    return render_template(
        'tracking_result.html', shipment=shipment, checkpoints=checkpoints, coords=coords_list,
        route_coords=route_coords, service_level=service_level, delivery_window=delivery_window,
        proof_of_delivery=proof_of_delivery, progress=progress,
        current_location=current_location, current_lat=current_lat, current_lon=current_lon,
        stage=stage, current_checkpoint_index=current_checkpoint_index,
        tawk_property_id=app.config['TAWK_PROPERTY_ID'], tawk_widget_id=app.config['TAWK_WIDGET_ID']
    )

@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    try:
        bot = get_bot()
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({'error': 'Invalid JSON payload'}), 400
        update = types.Update.de_json(data)
        bot.process_new_updates([update])
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        flask_logger.exception('Telegram webhook processing failed: %s', e)
        return jsonify({'error': str(e)}), 500

@app.route('/notify', methods=['POST'])
def websocket_notify():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON payload'}), 400

    try:
        socketio.emit('tracking_update', data, namespace='/')
        flask_logger.info('External notify payload delivered to socket clients')
        return jsonify({'success': True}), 200
    except Exception as e:
        flask_logger.warning(f'External notify failed: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health_check():
    """Health check endpoint - SMTP failures are non-critical."""
    status = {'status': 'healthy', 'database': 'ok', 'redis': 'ok', 'smtp': 'ok'}
    
    # Check database - CRITICAL
    try:
        db.session.execute(text('SELECT 1'))
    except Exception as e:
        status['status'] = status['database'] = 'error'
        flask_logger.exception("Health check database failed: %s", e)
    
    # Check Redis - CRITICAL
    try:
        if redis_client:
            redis_client.ping()
        else:
            status['redis'] = 'unavailable'
    except Exception as e:
        status['redis'] = 'error'
        flask_logger.exception("Health check redis failed: %s", e)
    
    # Check SMTP - NON-CRITICAL (just warn, don't fail)
    try:
        email_provider = 'resend' if app.config.get('RESEND_API_KEY') else app.config.get('EMAIL_PROVIDER', 'smtp')
        if email_provider == 'resend':
            if app.config.get('RESEND_API_KEY') and app.config.get('SMTP_FROM'):
                status['smtp'] = 'resend'
            else:
                status['smtp'] = 'unconfigured'
                flask_logger.warning("Resend email provider is not configured")
        elif app.config.get('SMTP_HOST') and app.config.get('SMTP_USER') and app.config.get('SMTP_PASS'):
            with open_smtp_connection() as s:
                s.noop()
        else:
            status['smtp'] = 'unconfigured'
            flask_logger.warning("SMTP not configured")
    except Exception as e:
        status['smtp'] = 'unavailable'  # Not 'error' - just unavailable
        flask_logger.warning("Health check smtp unavailable: %s", e)
    
    # Only return 500 if critical services are down
    critical_ok = status['database'] == 'ok' and status['redis'] != 'error'
    return jsonify(status), 200 if critical_ok else 500


@app.route('/debug/tn/<tracking_number>')
def debug_tracking_number(tracking_number):
    """Return a small JSON snapshot of Redis-backed live fields for a tracking number.
    Only enabled in debug/development mode."""
    # Allow in debug/development or when caller is localhost (safe for local debugging)
    remote = request.remote_addr
    if not (app.debug or app.config.get('FLASK_ENV') == 'development' or remote in ('127.0.0.1', '::1')):
        return jsonify({'error': 'Not available'}), 403
    tn = sanitize_tracking_number(tracking_number)
    if not tn:
        return jsonify({'error': 'Invalid tracking number'}), 400
    try:
        fields = ['progress', 'current_location', 'current_lat', 'current_lon', 'service_level', 'delivery_window', 'paused']
        snapshot = {}
        for f in fields:
            v = rget(f, tn, None)
            try:
                # try to coerce numeric
                if v is not None and isinstance(v, str) and v.replace('.', '', 1).isdigit():
                    snapshot[f] = float(v)
                else:
                    snapshot[f] = v
            except Exception:
                snapshot[f] = v
        return jsonify({'tracking_number': tn, 'snapshot': snapshot})
    except Exception as e:
        flask_logger.exception('Debug TN fetch failed: %s', e)
        return jsonify({'error': 'Failed to fetch'}), 500

@app.route('/debug')
def debug_info():
    status = {
        'app_debug': app.debug,
        'flask_env': app.config.get('FLASK_ENV'),
        'services_started': services_started,
        'database': 'unknown',
        'redis': 'unknown',
        'smtp': 'unknown',
        'bot': 'unknown',
        'webhook_url': config.webhook_url,
        'webhook_configured': bool(config.webhook_url),
        'redis_configured': bool(config.redis_url),
        'smtp_configured': bool(app.config.get('SMTP_HOST') and app.config.get('SMTP_USER') and app.config.get('SMTP_PASS'))
    }
    try:
        db.session.execute(text('SELECT 1'))
        status['database'] = 'ok'
    except Exception as e:
        status['database'] = 'error'
        flask_logger.exception("Debug database check failed: %s", e)
    try:
        if redis_client:
            redis_client.ping()
            status['redis'] = 'ok'
        else:
            status['redis'] = 'unavailable'
    except Exception as e:
        status['redis'] = 'error'
        flask_logger.exception("Debug redis check failed: %s", e)
    try:
        if app.config.get('SMTP_HOST') and app.config.get('SMTP_USER') and app.config.get('SMTP_PASS'):
            with open_smtp_connection() as s:
                s.noop()
            status['smtp'] = 'ok'
        else:
            status['smtp'] = 'unconfigured'
    except Exception as e:
        status['smtp'] = 'unavailable'  # Not 'error'
        flask_logger.warning("Debug smtp unavailable: %s", e)
    try:
        bot = get_bot()
        if bot:
            webhook_info = bot.get_webhook_info()
            status['bot'] = {
                'class': bot.__class__.__name__,
                'webhook_url': getattr(webhook_info, 'url', None)
            }
        else:
            status['bot'] = 'disabled'
    except Exception as e:
        status['bot'] = 'error'
        flask_logger.exception("Debug bot webhook info failed: %s", e)
    debug_config = {
        'SQLALCHEMY_DATABASE_URI': app.config.get('SQLALCHEMY_DATABASE_URI'),
        'WEBSOCKET_SERVER': app.config.get('WEBSOCKET_SERVER'),
        'GLOBAL_WEBHOOK_URL': app.config.get('GLOBAL_WEBHOOK_URL'),
        'WEBHOOK_URL': config.webhook_url,
        'ALLOWED_ADMINS': config.allowed_admins,
        'VALID_STATUSES': config.valid_statuses,
        'SMTP_HOST': app.config.get('SMTP_HOST'),
        'SMTP_PORT': app.config.get('SMTP_PORT'),
        'SMTP_FROM': app.config.get('SMTP_FROM'),
        'GEOCODING_API_KEY_SET': bool(app.config.get('GEOCODING_API_KEY')),
        'TAWK_PROPERTY_ID': app.config.get('TAWK_PROPERTY_ID'),
        'TAWK_WIDGET_ID': app.config.get('TAWK_WIDGET_ID')
    }
    return jsonify({'status': status, 'config': debug_config})


@app.route('/test/geocode')
def test_geocode():
    """Test geocoding with fallback."""
    address = request.args.get('address', 'Lagos, NG')
    
    # Try each provider separately
    results = {
        'input': address,
        'geoapify': geoapify_geocode_fallback(address),
        'maps_co': geocode_maps_co_fallback(address),
        'nominatim': nominatim_geocode_fallback(address),
        'known_locations': resolve_from_known_locations_fallback(address),
        'final_result': geocode_with_fallback(address)
    }
    
    # Check which API keys are configured
    results['api_keys'] = {
        'geoapify': bool(app.config.get('GEOAPIFY_API_KEY') or app.config.get('GEOCODING_API_KEY')),
        'maps_co': bool(app.config.get('MAPS_CO_API_KEY') or app.config.get('GEOCODING_API_KEY'))
    }
    
    return jsonify(results)

# === ADMIN ROUTES ===
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form.get('password') == app.config['ADMIN_PASSWORD']:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        flash("Invalid password", "error")
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin/metrics')
@admin_required
def admin_metrics():
    metrics = {}
    try:
        active_keys = rkeys("clients:*")
        metrics['active_simulations'] = len(active_keys)
        speeds = rhgetall("sim_speed_multipliers") or {}
        if speeds:
            try:
                metrics['avg_speed'] = round(sum(float(v) for v in speeds.values()) / len(speeds), 2)
            except Exception:
                metrics['avg_speed'] = 0.0
        else:
            metrics['avg_speed'] = 0.0
        metrics['paused_simulations'] = rhlen("paused_simulations") if rexists("paused_simulations") else 0
    except Exception:
        metrics['active_simulations'] = 0
        metrics['avg_speed'] = 0.0
        metrics['paused_simulations'] = 0
    try:
        statuses = Shipment.query.with_entities(
            Shipment.status, db.func.count()
        ).group_by(Shipment.status).all()
        metrics['status_distribution'] = {s: c for s, c in statuses}
    except Exception:
        metrics['status_distribution'] = {}
    return jsonify(metrics)

# ============================================================
# FIXED ADMIN DASHBOARD - Uses direct database queries
# ============================================================
@app.route('/admin')
@admin_required
def admin_dashboard():
    page = int(request.args.get('page', 1))
    per_page = 10
    
    try:
        total = Shipment.query.count()
        flask_logger.info(f"Total shipments found: {total}")
        
        shipments_query = Shipment.query.order_by(
            Shipment.created_at.desc()
        ).paginate(page=page, per_page=per_page, error_out=False)
        
        shipments_data = []
        for s in shipments_query.items:
            try:
                paused = False
                speed = 1.0
                mode = "ground"
                progress = 0
                stage = "pickup"
                service_level = "DHL Express"
                delivery_window = "Calculating..."
                proof_of_delivery = "Pending"
                
                if redis_client:
                    try:
                        paused = rget("paused_simulations", s.tracking_number, "false") == "true"
                        speed = float(rget("sim_speed_multipliers", s.tracking_number, "1.0") or "1.0")
                        mode = rget("transport_mode", s.tracking_number, "ground") or "ground"
                        progress = float(rget("progress", s.tracking_number, "0") or "0")
                        stage = rget("stage", s.tracking_number, "pickup") or "pickup"
                        
                        service_level = rget("service_level", s.tracking_number, "DHL Express") or "DHL Express"
                        delivery_window = rget("delivery_window", s.tracking_number, "Calculating...") or "Calculating..."
                        proof_of_delivery = rget("proof_of_delivery", s.tracking_number, "Pending") or "Pending"
                    except Exception as redis_err:
                        flask_logger.warning(f"Redis error for {s.tracking_number}: {redis_err}")
                
                if isinstance(stage, bytes):
                    stage = stage.decode('utf-8')
                if isinstance(mode, bytes):
                    mode = mode.decode('utf-8')
                
                shipments_data.append({
                    'tracking_number': s.tracking_number,
                    'status': s.status,
                    'delivery_location': s.delivery_location,
                    'origin_location': s.origin_location,
                    'last_updated': s.last_updated.strftime("%Y-%m-%d %H:%M"),
                    'paused': paused,
                    'speed': f"{speed:.1f}x",
                    'mode': mode,
                    'carrier': s.carrier or 'DHL',
                    'service_level': service_level,
                    'delivery_window': delivery_window,
                    'proof_of_delivery': proof_of_delivery,
                    'recipient_email': s.recipient_email,
                    'progress_percent': progress * 100,
                    'stage': stage,
                    'email_notifications': s.email_notifications
                })
            except Exception as row_err:
                flask_logger.error(f"Error processing shipment {s.tracking_number}: {row_err}")
                continue
        
        total_pages = (total - 1) // per_page + 1 if total > 0 else 1
        
        return render_template('admin_dashboard.html',
                               total=total,
                               queue_len=redis_client.llen("notifications") if redis_client else 0,
                               active_clients=len(redis_client.keys("clients:*")) if redis_client else 0,
                               shipments=shipments_data,
                               page=page,
                               total_pages=total_pages,
                               now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    except Exception as e:
        flask_logger.error(f"Error in admin_dashboard: {e}")
        return render_template('admin_dashboard.html',
                               total=0,
                               queue_len=0,
                               active_clients=0,
                               shipments=[],
                               page=1,
                               total_pages=1,
                               error=str(e),
                               now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

@app.route('/admin/csv')
@admin_required
def admin_csv():
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Tracking Number", "Status", "Origin", "Destination", "Email", "Carrier", "Service Level", "Delivery Window", "Proof of Delivery", "Last Updated", "Created At"])
    for s in Shipment.query.order_by(Shipment.created_at.desc()).all():
        distance_km = estimate_distance(s.origin_location or "Lagos, NG", s.delivery_location)
        service_level = DHLRealisticSimulator.get_service_level(
            distance_km, DHLRealisticSimulator.is_business_hours(datetime.now())
        )
        delivery_window = DHLRealisticSimulator.get_delivery_window(service_level, distance_km)
        proof_of_delivery = DHLRealisticSimulator.generate_pod_info()
        writer.writerow([s.tracking_number, s.status, s.origin_location or "-", s.delivery_location,
                         s.recipient_email or "-", s.carrier, service_level, delivery_window, proof_of_delivery,
                         s.last_updated.strftime("%Y-%m-%d %H:%M"), s.created_at.strftime("%Y-%m-%d %H:%M")])
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": f"attachment;filename=shipments_{datetime.utcnow().strftime('%Y%m%d')}.csv"})

def generate_dhl_tracking():
    prefix = "JD"
    digits = ''.join(random.choices(string.digits, k=10))
    return f"{prefix}{digits}"

# ============================================================
# ADMIN API ENDPOINTS
# ============================================================

@app.route('/admin/api/shipment/<tn>')
@admin_required
def api_shipment_detail(tn):
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return jsonify({'error': 'Not found'}), 404
    
    speed = float(rget("sim_speed_multipliers", tn, "1.0") or "1.0")
    paused = rget("paused_simulations", tn, "false") == "true"
    lock_stage = rget("lock_stage", tn, "false") == "true"   # added
    mode = rget("transport_mode", tn, "ground") or "ground"
    delivery_attempt = int(rget("delivery_attempts", tn, "0") or "0")
    max_attempts = int(rget("max_attempts", tn, "3") or "3")
    progress = float(rget("progress", tn, "0") or "0")
    checkpoints = (shipment.checkpoints or "").split(";") if shipment.checkpoints else []
    
    service_level = rget("service_level", tn, "DHL Express") or "DHL Express"
    delivery_window = rget("delivery_window", tn, "") or ""
    proof_of_delivery = rget("proof_of_delivery", tn, "") or ""
    sim_days = rget("sim_days", tn, os.getenv('SIM_DEFAULT_DAYS', '10')) or os.getenv('SIM_DEFAULT_DAYS', '10')
    temperature = rget("temperature", tn, None)
    current_lat = rget('current_lat', tn, None)
    current_lon = rget('current_lon', tn, None)
    
    return jsonify({
        'tracking_number': shipment.tracking_number,
        'status': shipment.status,
        'origin_location': shipment.origin_location,
        'origin_lat': shipment.origin_lat,
        'origin_lon': shipment.origin_lon,
        'delivery_location': shipment.delivery_location,
        'delivery_lat': shipment.delivery_lat,
        'delivery_lon': shipment.delivery_lon,
        'carrier': shipment.carrier,
        'recipient_email': shipment.recipient_email,
        'checkpoints': checkpoints,
        'last_updated': shipment.last_updated.isoformat(),
        'speed_multiplier': speed,
        'paused': paused,
        'lock_stage': lock_stage,   # added
        'mode': mode,
        'delivery_attempt': delivery_attempt,
        'max_attempts': max_attempts,
        'service_level': service_level,
        'delivery_window': delivery_window,
        'proof_of_delivery': proof_of_delivery,
        'sim_days': float(sim_days) if str(sim_days).replace('.', '', 1).isdigit() else 10.0,
        'temperature': temperature,
        'progress': progress,
        'current_lat': float(current_lat) if current_lat is not None else None,
        'current_lon': float(current_lon) if current_lon is not None else None
    })


def purge_shipment_cache(tn):
    if not redis_client or not tn:
        return
    try:
        redis_client.hdel(
            'paused_simulations', tn,
            'sim_speed_multipliers', tn,
            'transport_mode', tn,
            'delivery_attempts', tn,
            'max_attempts', tn,
            'progress', tn,
            'stage', tn,
            'delivery_window', tn,
            'proof_of_delivery', tn,
            'service_level', tn,
            'lock_stage', tn   # added
        )
        redis_client.delete(f'email_history:{tn}', f'clients:{tn}')
    except Exception:
        pass


def reset_db_session():
    try:
        db.session.rollback()
    except Exception:
        pass
    try:
        db.session.remove()
    except Exception:
        pass


def reload_shipment(tn):
    if not tn:
        return None
    reset_db_session()
    try:
        return Shipment.query.filter_by(tracking_number=tn).first()
    except Exception:
        return None

@app.route('/admin/api/shipment/<tn>/update', methods=['POST'])
@admin_required
def api_shipment_update(tn):
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return jsonify({'error': 'Not found'}), 404

    data = request.get_json() or {}
    editable = {
        'status', 'stage', 'service_level', 'delivery_window', 'proof_of_delivery',
        'recipient_email', 'delivery_location', 'paused', 'speed', 'days', 'email_notifications', 'checkpoints',
        'lock_stage'   # added
    }

    updated = {}
    try:
        for k, v in data.items():
            if k not in editable:
                continue
            if k == 'paused':
                if redis_client:
                    rset('paused_simulations', tn, 'true' if v else 'false')
                updated['paused'] = bool(v)
                continue
            if k == 'speed':
                try:
                    speed = float(v)
                except Exception:
                    speed = 1.0
                if redis_client:
                    rset('sim_speed_multipliers', tn, str(speed))
                updated['speed'] = speed
                continue
            if k == 'stage':
                if redis_client:
                    rset('stage', tn, v)
                updated['stage'] = v
                continue
            if k == 'service_level' and redis_client:
                rset('service_level', tn, v)
            if k == 'delivery_window' and redis_client:
                rset('delivery_window', tn, v)
            if k == 'proof_of_delivery' and redis_client:
                rset('proof_of_delivery', tn, v)
            if k == 'days':
                try:
                    days_value = float(v)
                except Exception:
                    days_value = float(os.getenv('SIM_DEFAULT_DAYS', '10'))
                days_value = max(1.0, min(365.0, days_value))
                if redis_client:
                    rset('sim_days', tn, str(days_value))
                updated['days'] = days_value
                continue
            if k == 'lock_stage':   # added
                if redis_client:
                    rset('lock_stage', tn, 'true' if v else 'false')
                updated['lock_stage'] = bool(v)
                continue

            if k == 'checkpoints':
                if isinstance(v, list):
                    shipment.checkpoints = ';'.join(v)
                    updated['checkpoints'] = v
                continue

            if hasattr(shipment, k):
                if k in ('delivery_location', 'origin_location') and v:
                    name, coords = resolve_location(v)
                    v = name
                    if coords:
                        if k == 'delivery_location':
                            shipment.delivery_lat = coords.get('lat')
                            shipment.delivery_lon = coords.get('lon')
                        else:
                            shipment.origin_lat = coords.get('lat')
                            shipment.origin_lon = coords.get('lon')
                    else:
                        return jsonify({'error': f'Could not resolve location for {k}'}), 400
                setattr(shipment, k, v)
                updated[k] = v

        shipment.last_updated = datetime.now()
        db.session.commit()
        invalidate_cache(tn)
        try:
            broadcast_update(tn)
        except Exception:
            pass
        return jsonify({'success': True, 'updated': updated})
    except Exception as e:
        flask_logger.exception('Failed to update shipment %s: %s', tn, e)
        return jsonify({'error': str(e)}), 500

@app.route('/admin/api/shipment/<tn>/delete', methods=['POST'])
@admin_required
def api_delete_shipment(tn):
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return jsonify({'error': 'Not found'}), 404
    try:
        db.session.delete(shipment)
        db.session.commit()
        purge_shipment_cache(tn)
        return jsonify({'success': True, 'deleted': tn})
    except Exception as e:
        db.session.rollback()
        flask_logger.exception('Failed to delete shipment %s: %s', tn, e)
        return jsonify({'error': 'Failed to delete shipment'}), 500

@app.route('/admin/api/cities')
@admin_required
def api_cities():
    cities = sorted(set(list(KNOWN_LOCATION_COORDS.keys()) + list(DHLRealisticSimulator.DHL_HUBS.keys()) + [
        "Lagos, NG", "Abuja, NG", "Port Harcourt, NG", "Kano, NG", "Ibadan, NG",
        "New York, NY", "Los Angeles, CA", "London, UK", "Dubai, UAE",
        "Tokyo, JP", "Sydney, AU", "Paris, FR", "Berlin, DE", "Mumbai, IN",
        "Singapore, SG", "Hong Kong, HK", "São Paulo, BR", "Johannesburg, ZA",
        "Cairo, EG", "Moscow, RU", "Toronto, CA", "Mexico City, MX", "Seoul, KR",
        "Bangkok, TH", "Jakarta, ID", "Delhi, IN", "Beijing, CN", "Shanghai, CN",
        "Istanbul, TR", "Karachi, PK", "Buenos Aires, AR", "Rio de Janeiro, BR",
        "Tel Aviv, IL", "Jerusalem, IL", "Haifa, IL", "Eilat, IL", "Rehovot, IL",
        "Rishon LeZion, IL", "Petah Tikva, IL", "Ashdod, IL", "Ashkelon, IL",
        "Beersheba, IL", "Netanya, IL", "Holon, IL", "Bnei Brak, IL", "Herzliya, IL",
        "Kfar Saba, IL", "Ra'anana, IL", "Modiin, IL", "Nazareth, IL", "Tiberias, IL",
        "Acre, IL", "Nahariya, IL", "Safed, IL", "Kiryat Shmona, IL", "Caesarea, IL",
        "Athens, GR", "Lisbon, PT", "Stockholm, SE", "Oslo, NO",
        "Helsinki, FI", "Warsaw, PL", "Prague, CZ", "Budapest, HU", "Vienna, AT",
        "Zurich, CH", "Amsterdam, NL", "Brussels, BE", "Dublin, IE", "Madrid, ES",
        "Rome, IT", "Milan, IT", "Barcelona, ES", "Cincinnati, OH", "Miami, FL",
        "Frankfurt, DE", "Leipzig, DE"
    ]))
    return jsonify(cities)

@app.route('/admin/api/city_coords')
@admin_required
def api_city_coords():
    coords = {
        name: {'lat': float(value['lat']), 'lon': float(value['lon'])}
        for name, value in KNOWN_LOCATION_COORDS.items()
    }
    for name, value in DHLRealisticSimulator.DHL_HUBS.items():
        coords.setdefault(name, {'lat': float(value['lat']), 'lon': float(value['lon'])})
    return jsonify(coords)


@app.route('/admin/api/geocoding-status')
@admin_required
def admin_geocoding_status():
    """Check the status of all geocoding providers."""
    test_addresses = [
        "Lagos, NG",
        "London, UK", 
        "New York, NY",
        "Dubai, UAE",
        "Rehovot, IL",
        "Tokyo, JP"
    ]
    
    results = {}
    for address in test_addresses:
        result = geocode_with_fallback(address)
        results[address] = {
            'success': result is not None,
            'formatted': result.get('formatted') if result else None,
            'provider': result.get('provider') if result else None,
            'coordinates': f"({result.get('lat')}, {result.get('lon')})" if result else None
        }
    
    # Check API key status
    geoapify_key = app.config.get('GEOAPIFY_API_KEY') or app.config.get('GEOCODING_API_KEY')
    maps_co_key = app.config.get('MAPS_CO_API_KEY') or app.config.get('GEOCODING_API_KEY')
    
    return jsonify({
        'providers': {
            'geoapify': {
                'configured': bool(geoapify_key),
                'key_preview': geoapify_key[:8] + '...' if geoapify_key else None
            },
            'maps_co': {
                'configured': bool(maps_co_key),
                'key_preview': maps_co_key[:8] + '...' if maps_co_key else None
            },
            'nominatim': {
                'configured': True,
                'note': 'Free, rate limited to 1 request/second'
            },
            'known_locations': {
                'configured': True,
                'count': len(KNOWN_LOCATION_COORDS)
            }
        },
        'test_results': results,
        'rate_limiter_status': {
            api: len(timestamps) for api, timestamps in geocode_rate_limiter.items()
        }
    })

def create_shipment_record(origin, destination, recipient_email=None, service_level='DHL Express'):
    origin = origin.strip() if isinstance(origin, str) else origin
    destination = destination.strip() if isinstance(destination, str) else destination
    service_level = service_level.strip() if isinstance(service_level, str) else service_level
    recipient_email = recipient_email.strip() if isinstance(recipient_email, str) else recipient_email

    if not origin or not destination:
        return {'error': 'Origin and destination required', 'error_code': 'missing_fields'}, 400

    valid_service_levels = set(DHLRealisticSimulator.SERVICE_LEVELS.keys())
    if service_level not in valid_service_levels:
        return {'error': 'Invalid service_level', 'allowed': sorted(valid_service_levels), 'error_code': 'invalid_service_level'}, 400

    if recipient_email and not validate_email(recipient_email):
        return {'error': 'Invalid recipient_email', 'error_code': 'invalid_recipient_email'}, 400

    tracking_number = generate_dhl_tracking()
    while Shipment.query.filter_by(tracking_number=tracking_number).first():
        tracking_number = generate_dhl_tracking()

    now = datetime.now()
    norm_origin, origin_coords = resolve_location(origin)
    norm_destination, dest_coords = resolve_location(destination)

    if not origin_coords or not dest_coords:
        failed_locations = []
        if not origin_coords:
            failed_locations.append(f"origin '{origin}'")
        if not dest_coords:
            failed_locations.append(f"destination '{destination}'")
        # Try a heuristic: strip street/house-number, transliterate, and attempt city+country fallback
        import re
        import unicodedata

        def _simplify_to_city_cc(addr):
            if not addr or not isinstance(addr, str):
                return None
            s = addr.strip()
            # Normalize unicode to remove accents when possible
            try:
                s = unicodedata.normalize('NFKD', s)
                s = ''.join(c for c in s if not unicodedata.combining(c))
            except Exception:
                pass

            # Remove common street prefixes/suffixes and house numbers
            s = re.sub(r"\b(st|street|rd|road|ave|avenue|blvd|lane|ln|way|drive|dr|ha|ha')\b", ' ', s, flags=re.IGNORECASE)
            s = re.sub(r"\d+[A-Za-z\-/]*", ' ', s)  # remove numbers like '12', '12A', '12-4'
            s = re.sub(r"[^A-Za-z0-9, ]", ' ', s)
            s = re.sub(r"\s+", ' ', s).strip()

            parts = [p.strip() for p in s.split(',') if p.strip()]
            # If we have at least two comma parts, assume last two are city and country code/name
            if len(parts) >= 2:
                cand = f"{parts[-2].title()}, {parts[-1].upper()}"
                return cand

            toks = s.split()
            # Try to match tokens against known locations and DHL hubs
            try:
                known_candidates = set()
                try:
                    known_candidates.update(k for k in KNOWN_LOCATION_COORDS.keys())
                except Exception:
                    pass
                try:
                    known_candidates.update(k for k in DHLRealisticSimulator.DHL_HUBS.keys())
                except Exception:
                    pass
                lower_to_known = {k.lower(): k for k in known_candidates}
                for tok in toks[::-1]:
                    if not tok:
                        continue
                    # If token looks like a country code (2 letters), prefer exact country-code suffix matches
                    if len(tok) <= 2:
                        # collect same-country candidates and score them by other token matches
                        candidates = [k for k in known_candidates if k.endswith(', ' + tok.upper())]
                        if candidates:
                            best = None
                            best_score = 0
                            for cand in candidates:
                                score = 0
                                for t in toks:
                                    if len(t) > 2 and t.lower() in cand.lower():
                                        score += 1
                                if score > best_score:
                                    best_score = score
                                    best = cand
                            if best and best_score > 0:
                                return best
                        # skip very short tokens for substring matching if no scored candidate
                        continue
                    for known in known_candidates:
                        if tok.lower() in known.lower():
                            return known
                # also try full string match
                name = ' '.join(toks).lower()
                for known in known_candidates:
                    if name in known.lower() or known.lower() in name:
                        return known
                # Check curated transliteration map for high-value cities
                try:
                    for tok in toks:
                        if not tok:
                            continue
                        key_raw = tok.lower()
                        key_unidecode = _unidecode(tok).lower() if callable(_unidecode) else tok.lower()
                        if key_raw in HIGH_VALUE_TRANSLITERATION_MAP:
                            return HIGH_VALUE_TRANSLITERATION_MAP[key_raw]
                        if key_unidecode in HIGH_VALUE_TRANSLITERATION_MAP:
                            return HIGH_VALUE_TRANSLITERATION_MAP[key_unidecode]
                    # also check full transliterated phrase
                    full_key = _unidecode(' '.join(toks)).lower()
                    if full_key in HIGH_VALUE_TRANSLITERATION_MAP:
                        return HIGH_VALUE_TRANSLITERATION_MAP[full_key]
                except Exception:
                    pass
                # Use RapidFuzz fuzzy matching if available to find the best-known candidate
                try:
                    if fuzz and toks:
                        query = ' '.join(toks)
                        best = None
                        best_score = 0
                        for known in known_candidates:
                            try:
                                score = fuzz.partial_ratio(_unidecode(known).lower(), _unidecode(query).lower())
                                if score > best_score:
                                    best_score = score
                                    best = known
                            except Exception:
                                continue
                        if best and best_score >= RAPIDFUZZ_THRESHOLD:
                            return best
                except Exception:
                    pass
                # Last-resort: prefix match on transliterated tokens to catch script mismatches
                try:
                    if toks:
                        longest = max(toks, key=len)
                        prefix = longest[:4].lower()
                        for known in known_candidates:
                            try:
                                if prefix and prefix in _unidecode(known).lower():
                                    return known
                            except Exception:
                                continue
                except Exception:
                    pass
            except Exception:
                pass

            if len(toks) >= 2:
                # If last token looks like country code (2 letters), use it
                last = toks[-1].upper()
                if len(last) == 2:
                    city = ' '.join(toks[:-1]).title()
                    return f"{city}, {last}"
                # Otherwise, return last two tokens as city + country-code-like token
                city = toks[-2].title()
                country_like = toks[-1].upper()
                return f"{city}, {country_like}"

            return None

        # Attempt to salvage by simplifying failing addresses
        salvaged = False
        if not origin_coords:
            simp = _simplify_to_city_cc(origin)
            if simp:
                flask_logger.info(f"Attempting simplified origin geocode: '{simp}'")
                n_origin, o_coords = resolve_location(simp)
                if o_coords:
                    origin_coords = o_coords
                    norm_origin = n_origin
                    salvaged = True
        if not dest_coords:
            simp = _simplify_to_city_cc(destination)
            if simp:
                flask_logger.info(f"Attempting simplified destination geocode: '{simp}'")
                n_dest, d_coords = resolve_location(simp)
                if d_coords:
                    dest_coords = d_coords
                    norm_destination = n_dest
                    salvaged = True

        if not salvaged:
            flask_logger.warning(
                "Shipment geocoding failed: %s (GEOCODING_API_KEY configured=%s)",
                ', '.join(failed_locations),
                bool(app.config.get('GEOCODING_API_KEY'))
            )
            return {
                'error': 'Unable to resolve location coordinates',
                'error_code': 'geocoding_failed',
                'details': f"Could not resolve {', '.join(failed_locations)}. Use 'City, Country Code' or configure GEOCODING_API_KEY."
            }, 400

    checkpoints = f"{now.strftime('%Y-%m-%d %H:%M')} - {norm_origin} - Shipment information received"

    shipment = Shipment(
        tracking_number=tracking_number,
        status='Pending',
        checkpoints=checkpoints,
        origin_location=norm_origin,
        origin_lat=origin_coords.get('lat') if origin_coords else None,
        origin_lon=origin_coords.get('lon') if origin_coords else None,
        delivery_location=norm_destination,
        delivery_lat=dest_coords.get('lat') if dest_coords else None,
        delivery_lon=dest_coords.get('lon') if dest_coords else None,
        last_updated=now,
        recipient_email=recipient_email or '',
        created_at=now,
        carrier='DHL',
        email_notifications=bool(recipient_email)
    )

    try:
        db.session.add(shipment)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flask_logger.error(f"Failed to save shipment {tracking_number}: {e}")
        return {'error': 'Failed to save shipment to database', 'error_code': 'db_save_failed'}, 500

    distance = None
    try:
        if origin_coords and dest_coords:
            lat1, lon1 = origin_coords['lat'], origin_coords['lon']
            lat2, lon2 = dest_coords['lat'], dest_coords['lon']
            from math import radians, sin, cos, sqrt, atan2
            rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
            dlon = rlon2 - rlon1
            dlat = rlat2 - rlat1
            a = sin(dlat/2)**2 + cos(rlat1) * cos(rlat2) * sin(dlon/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            distance = round(6371 * c, 1)
        else:
            distance = estimate_distance(norm_origin, norm_destination)
    except Exception:
        distance = estimate_distance(norm_origin, norm_destination)

    mode = 'air' if distance > 1000 else 'ground'
    max_attempts = 3 if random.random() < 0.15 else 1
    delivery_window = DHLRealisticSimulator.get_delivery_window(service_level, distance)

    if redis_client:
        try:
            rset('service_level', tracking_number, service_level)
            rset('transport_mode', tracking_number, mode)
            rset('delivery_attempts', tracking_number, '0')
            rset('max_attempts', tracking_number, str(max_attempts))
            rset('progress', tracking_number, '0')
            rset('stage', tracking_number, 'pickup')
            rset('delivery_window', tracking_number, delivery_window)
            rset('proof_of_delivery', tracking_number, 'Pending')
        except Exception as redis_err:
            flask_logger.warning(f"Redis error for {tracking_number}: {redis_err}")

    try:
        if can_start_simulation():
            spawn_simulation(tracking_number)
        else:
            flask_logger.info(f"Simulator throttle active; skipping create_shipment launch for {tracking_number}")
    except Exception:
        try:
            if can_start_simulation():
                eventlet.spawn(simulate_tracking, tracking_number)
            else:
                flask_logger.info(f"Simulator throttle active; skipping create_shipment eventlet spawn for {tracking_number}")
        except Exception as sim_err:
            flask_logger.warning(f"Simulation start error for {tracking_number}: {sim_err}")

    return {
        'success': True,
        'tracking_number': tracking_number,
        'shipment': {
            'tracking_number': tracking_number,
            'status': 'Pending',
            'origin': origin,
            'destination': destination,
            'service_level': service_level,
            'mode': mode,
            'delivery_window': delivery_window if delivery_window else 'Calculating...'
        }
    }, 201

@app.route('/admin/api/create_shipment', methods=['POST'])
@admin_required
def api_create_shipment():
    data = request.get_json() or {}
    result, status_code = create_shipment_record(
        data.get('origin'),
        data.get('destination'),
        data.get('recipient_email'),
        data.get('service_level', 'DHL Express')
    )
    # Log payload and validation details for easier debugging from the admin UI
    if status_code != 201:
        try:
            flask_logger.warning("create_shipment failed: status=%s payload=%s error=%s", status_code, data, result)
        except Exception:
            flask_logger.warning("create_shipment failed: status=%s (failed to serialize payload/result)", status_code)
    return jsonify(result), status_code

@app.route('/admin/api/bulk_create', methods=['POST'])
@admin_required
def api_bulk_create():
    payload = request.get_json() or {}
    shipments = payload.get('shipments') or []
    if not isinstance(shipments, list) or not shipments:
        return jsonify({'error': 'Shipments list required'}), 400

    created = []
    errors = []
    for index, shipment_data in enumerate(shipments):
        if not isinstance(shipment_data, dict):
            errors.append({'index': index, 'error': 'Invalid shipment object'})
            continue

        origin = shipment_data.get('origin')
        destination = shipment_data.get('destination')
        recipient_email = shipment_data.get('recipient_email')
        service_level = shipment_data.get('service_level', 'DHL Express')

        result, status_code = create_shipment_record(origin, destination, recipient_email, service_level)
        if result.get('success'):
            created.append(result['tracking_number'])
        else:
            errors.append({'index': index, 'error': result.get('error', 'Unknown error'), 'status': status_code})

    return jsonify({
        'success': len(errors) == 0,
        'created': created,
        'errors': errors,
        'total_created': len(created),
        'total_errors': len(errors)
    }), 200

@app.route('/admin/api/shipments/email-history/<tn>')
@admin_required
def api_email_history(tn):
    if not tn:
        return jsonify([])
    history_key = f"email_history:{tn}"
    entries = []
    if redis_client:
        try:
            raw = redis_client.lrange(history_key, 0, 99) or []
            for item in raw:
                if isinstance(item, bytes):
                    item = item.decode('utf-8')
                try:
                    entries.append(json.loads(item))
                except Exception:
                    continue
        except Exception:
            pass
    return jsonify(entries)

# ✅ UPDATED: Admin Send Email – now uses the rich DHL HTML template
@app.route('/admin/api/send_email', methods=['POST'])
@admin_required
def api_send_email():
    data = request.get_json() or {}
    tn = data.get('tracking_number')
    email_type = data.get('email_type', 'status_update')
    custom_message = data.get('custom_message', '')

    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        return jsonify({'error': 'Shipment not found'}), 404
    if not shipment.recipient_email:
        return jsonify({'error': 'No recipient email on file'}), 400

    # Get additional details from Redis or compute them
    service_level = rget("service_level", tn, "DHL Express") or "DHL Express"
    delivery_window = rget("delivery_window", tn, "Calculating...") or "Calculating..."
    checkpoints = (shipment.checkpoints or "").split(";") if shipment.checkpoints else []
    latest_checkpoint = checkpoints[-1] if checkpoints else "Shipment information received"

    # Build the rich DHL HTML template
    html_body = build_dhl_email_html(
        tn,
        shipment.status,
        latest_checkpoint,
        shipment.delivery_location,
        service_level=service_level,
        delivery_window=delivery_window
    )

    # Add custom message if provided, placed above the track button
    if custom_message:
        custom_html = f'<p style="margin: 1rem 0; padding: 1rem; background: #f0f9ff; border-radius: 6px; border-left: 4px solid #D40511;">{custom_message}</p>'
        # Insert it before the "Track Shipment" button (which is preceded by <hr>)
        html_body = html_body.replace(
            '<hr style="border:0;border-top:1px solid #e5e7eb;margin:1.25rem 0;">',
            f'{custom_html}<hr style="border:0;border-top:1px solid #e5e7eb;margin:1.25rem 0;">'
        )

    plain_body = (
        f"DHL Update: {tn}\n"
        f"Status: {shipment.status}\n"
        f"Location: {latest_checkpoint.split(' - ')[1] if ' - ' in latest_checkpoint else shipment.delivery_location}\n"
        f"Service: {service_level}\n"
        f"Estimated Delivery: {delivery_window or 'Pending'}\n"
        f"Track: {app.config['WEBSOCKET_SERVER']}/track/{tn}"
    )

    # Use existing send_email_notification (it handles SMTP/Resend)
    success = send_email_notification(
        shipment.recipient_email,
        f"DHL Shipment {tn} - {email_type.replace('_', ' ').title()}",
        html_body=html_body,
        plain_body=plain_body,
        tracking_number=tn,
        email_type=email_type,
        message=custom_message or "Shipment update"
    )

    if success:
        return jsonify({'success': True, 'recipient': shipment.recipient_email})
    else:
        # Fallback: enqueue the notification
        enqueue_notification({
            "tracking_number": tn,
            "type": "email",
            "data": {
                "recipient_email": shipment.recipient_email,
                "subject": f"DHL Shipment {tn} - {email_type.replace('_', ' ').title()}",
                "html_body": html_body,
                "plain_body": plain_body
            }
        })
        return jsonify({'success': False, 'enqueued': True, 'message': 'Email send failed; notification enqueued'}), 202

@app.route('/admin/api/notifications/count')
@admin_required
def api_notifications_count():
    """Return the number of pending notifications in the Redis queue."""
    try:
        if not redis_client:
            return jsonify({'count': 0})
        cnt = 0
        try:
            cnt = int(redis_client.llen('notifications') or 0)
        except Exception:
            # redis-py may return bytes/str; coerce
            try:
                raw = redis_client.execute_command('LLEN', 'notifications')
                cnt = int(raw or 0)
            except Exception:
                cnt = 0
        return jsonify({'count': cnt})
    except Exception as e:
        flask_logger.error(f"Failed to read notifications count: {e}")
        flask_logger.debug(traceback.format_exc())
        return jsonify({'count': 0}), 500

@app.route('/admin/api/pause', methods=['POST'])
@admin_required
def api_pause_simulation():
    data = request.get_json() or {}
    tn = data.get('tracking_number')
    pause = data.get('pause')
    if not tn or pause is None:
        return jsonify({'error': 'tracking_number and pause required'}), 400

    if redis_client:
        try:
            rset('paused_simulations', tn, 'true' if bool(pause) else 'false')
            invalidate_cache(tn)
            try:
                broadcast_update(tn)
            except Exception:
                pass
        except Exception as e:
            flask_logger.warning(f"Failed to pause/resume shipment {tn}: {e}")
            return jsonify({'error': 'Failed to update pause state'}), 500

    return jsonify({'success': True, 'paused': bool(pause)})

@app.route('/admin/api/speed', methods=['POST'])
@admin_required
def api_update_speed():
    data = request.get_json() or {}
    tn = data.get('tracking_number')
    speed = data.get('speed')
    if not tn or speed is None:
        return jsonify({'error': 'tracking_number and speed required'}), 400

    try:
        speed_value = float(speed)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid speed value'}), 400

    speed_value = max(0.1, min(10.0, speed_value))
    if redis_client:
        try:
            rset('sim_speed_multipliers', tn, str(speed_value))
            invalidate_cache(tn)
            try:
                broadcast_update(tn)
            except Exception:
                pass
        except Exception as e:
            flask_logger.warning(f"Failed to update speed for {tn}: {e}")
            return jsonify({'error': 'Failed to update speed state'}), 500

    return jsonify({'success': True, 'speed': speed_value})

# SocketIO - Merged disconnect handlers
@socketio.on('connect')
def on_connect():
    sid = getattr(request, 'sid', None)
    try:
        headers = dict(request.headers)
    except Exception:
        headers = {}
    transport = request.args.get('transport') or (request.environ.get('wsgi.websocket') and 'websocket') or 'polling'
    details = {
        'event': 'connect',
        'sid': sid,
        'addr': request.remote_addr,
        'transport': transport,
        'headers': {k: headers.get(k) for k in ['User-Agent', 'Origin', 'Referer'] if headers.get(k)},
        'query': request.args.to_dict(flat=False)
    }
    flask_logger.info("SocketIO connect: %s", details)
    try:
        add_socket_event(details)
    except Exception:
        pass
    emit('status', {'message': 'Connected'})


@socketio.on('disconnect')
def on_disconnect():
    sid = getattr(request, 'sid', None)
    details = {'event': 'disconnect', 'sid': sid, 'addr': request.remote_addr}
    flask_logger.info("SocketIO disconnect: %s", details)
    try:
        add_socket_event(details)
    except Exception:
        pass
    
    # Clean up clients
    for tn in list(in_memory_clients.keys()):
        remove_client(tn, request.sid)
    if redis_client:
        for key in redis_client.scan_iter("clients:*"):
            try:
                tn = key.decode().split(":", 1)[1]
                remove_client(tn, request.sid)
            except Exception:
                continue


@app.route('/admin/client_error', methods=['POST'])
@admin_required
def admin_client_error():
    payload = request.get_json(silent=True) or {}
    payload['remote_addr'] = request.remote_addr
    try:
        add_client_error(payload)
    except Exception:
        pass
    flask_logger.error('Client-side error reported: %s', payload)
    return jsonify({'success': True})

# ============================================================
# SOCKET.IO REQUEST_TRACKING (UPDATED with stage + current_checkpoint_index + route fallback)
# ============================================================
@socketio.on('request_tracking')
def on_request(data):
    tn = sanitize_tracking_number(data.get('tracking_number'))
    if not tn:
        emit('tracking_update', {'error': 'Invalid'})
        return
    shipment = Shipment.query.filter_by(tracking_number=tn).first()
    if not shipment:
        emit('tracking_update', {'error': 'Not found'})
        return
    add_client(tn, request.sid)
    checkpoints = (shipment.checkpoints or "").split(";")
    coords = geocode_locations(checkpoints)
    # --- FALLBACK: use origin/destination if checkpoints fail ---
    if len(coords) < 2:
        origin_coords = None
        dest_coords = None
        if shipment.origin_lat is not None and shipment.origin_lon is not None:
            origin_coords = {
                'lat': shipment.origin_lat,
                'lon': shipment.origin_lon,
                'desc': shipment.origin_location or 'Origin'
            }
        if shipment.delivery_lat is not None and shipment.delivery_lon is not None:
            dest_coords = {
                'lat': shipment.delivery_lat,
                'lon': shipment.delivery_lon,
                'desc': shipment.delivery_location or 'Destination'
            }
        if origin_coords and dest_coords:
            coords = [origin_coords, dest_coords]
        elif origin_coords:
            coords = [origin_coords]
        elif dest_coords:
            coords = [dest_coords]
    # --- end fallback ---
    route_coords = build_route_from_checkpoints(coords, mode='drive')
    try:
        dens_km = float(os.getenv('SIM_ROUTE_DENSIFY_KM', '1.0') or '1.0')
        route_coords = densify_route_coords(route_coords, dens_km)
    except Exception:
        pass
    speed = float(rget("sim_speed_multipliers", tn, "1.0") or "1.0")
    paused = rget("paused_simulations", tn, "false") == "true"
    progress = float(rget("progress", tn, "0") or "0")
    mode = rget("transport_mode", tn) or ("air" if estimate_distance(shipment.origin_location or "Lagos, NG", shipment.delivery_location) > 1000 else "ground")
    distance_km = estimate_distance(shipment.origin_location or "Lagos, NG", shipment.delivery_location)
    service_level = DHLRealisticSimulator.get_service_level(
        distance_km, DHLRealisticSimulator.is_business_hours(datetime.now())
    )
    delivery_window = DHLRealisticSimulator.get_delivery_window(service_level, distance_km)
    proof_of_delivery = DHLRealisticSimulator.generate_pod_info()
    current_location = rget('current_location', tn, '') or ''
    current_lat = rget('current_lat', tn, None)
    current_lon = rget('current_lon', tn, None)
    last_updated = shipment.last_updated.isoformat() if shipment.last_updated else None
    stage = rget('stage', tn, 'pickup') or 'pickup'

    # Compute current checkpoint index
    current_checkpoint_index = 0
    if checkpoints:
        current_checkpoint_index = min(int(progress * len(checkpoints)), len(checkpoints) - 1)

    emit('tracking_update', {
        'tracking_number': tn, 'status': shipment.status, 'delivery_location': shipment.delivery_location,
        'checkpoints': checkpoints, 'coords': [{'lat': c['lat'], 'lon': c['lon'], 'desc': c['desc']} for c in coords],
        'route_coords': route_coords, 'service_level': service_level, 'delivery_window': delivery_window,
        'proof_of_delivery': proof_of_delivery, 'progress': progress,
        'current_location': current_location, 'current_lat': float(current_lat) if current_lat is not None else None,
        'current_lon': float(current_lon) if current_lon is not None else None,
        'last_updated': last_updated,
        'speed_multiplier': speed, 'paused': paused, 'mode': mode, 'carrier': shipment.carrier,
        'stage': stage,
        'current_checkpoint_index': current_checkpoint_index
    })

services_started = False
services_started_lock = threading.Lock()

def start_background_services():
    global services_started
    with services_started_lock:
        if services_started:
            flask_logger.debug("Background services already started")
            return
        services_started = True

    try:
        flask_logger.info("Starting background services")
        with app.app_context():
            db.create_all()
        init_db()
        cache_route_templates()
        try:
            with app.app_context():
                active_shipments = Shipment.query.filter(Shipment.status.notin_(["Delivered", "Returned"])).order_by(Shipment.last_updated.desc()).limit(8).all()
                for index, s in enumerate(active_shipments):
                    try:
                        if not can_start_simulation():
                            flask_logger.info("Simulator throttle reached; stopping resume fan-out")
                            break
                        flask_logger.info(f"Resuming simulation for {s.tracking_number}")
                        spawn_simulation(s.tracking_number)
                    except Exception as e:
                        flask_logger.warning(f"Failed to spawn simulation for {s.tracking_number}: {e}")
        except Exception:
            pass
        eventlet.spawn(keep_alive)
        # --- FIX: Start the queue processor UNLESS DISABLE_QUEUE_PROCESSOR is set ---
        if os.getenv('DISABLE_QUEUE_PROCESSOR', '').lower() not in ('1', 'true', 'yes'):
            flask_logger.info("Starting notification queue processor (no separate worker detected)")
            eventlet.spawn(process_notification_queue)
        else:
            flask_logger.info("Queue processor disabled by DISABLE_QUEUE_PROCESSOR (using external worker)")
        eventlet.spawn(cleanup_websocket_clients)
    except Exception:
        with services_started_lock:
            services_started = False
        raise

@app.before_request
def ensure_background_services():
    if app.testing or os.getenv('SKIP_BACKGROUND_SERVICES', 'false').strip().lower() in ('1', 'true', 'yes'):
        return
    if not services_started:
        start_background_services()


@app.route('/admin/debug')
@admin_required
def admin_debug():
    """Return quick health/status info useful for debugging the admin UI."""
    info = {
        'services_started': services_started,
        'sqlite_url': app.config.get('SQLALCHEMY_DATABASE_URI'),
        'webrtc_server': app.config.get('WEBSOCKET_SERVER'),
        'redis_configured': bool(redis_client),
        'active_clients_in_memory': len(in_memory_clients) if in_memory_clients else 0,
        'shipments_count': None,
    }
    try:
        info['shipments_count'] = Shipment.query.count()
    except Exception as e:
        info['shipments_count'] = f'error: {e}'
    try:
        if redis_client:
            info['redis_paused_count'] = redis_client.hlen('paused_simulations') if redis_client.exists('paused_simulations') else 0
    except Exception:
        info['redis_paused_count'] = 'error'
    try:
        info['recent_socket_events'] = list(recent_socket_events)[:50]
    except Exception:
        info['recent_socket_events'] = []
    try:
        info['recent_client_errors'] = list(recent_client_errors)[:50]
    except Exception:
        info['recent_client_errors'] = []
    return jsonify(info)

# Start
if __name__ == '__main__':
    start_background_services()
    # Respect platform-provided PORT (e.g. Render/GCP); fall back to 10000 for local dev
    port_env = os.getenv('PORT') or app.config.get('PORT')
    try:
        port = int(port_env) if port_env else 10000
    except Exception:
        port = 10000
    flask_logger.info(f"Detected service running on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=os.getenv('FLASK_ENV') == 'development')
