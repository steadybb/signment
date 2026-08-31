# simulator_engine.py
from __future__ import annotations

import os
import random
import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from math import radians, sin, cos, sqrt, atan2
from typing import Callable, Optional, List, Tuple, Dict, Any


# ---------------------------------------------------------------------------
# Configuration – can be overridden by environment variables
# ---------------------------------------------------------------------------
GROUND_AVG_SPEED_KMH = float(os.getenv('SIM_GROUND_SPEED_KMH', '55.0'))
AIR_AVG_SPEED_KMH = float(os.getenv('SIM_AIR_SPEED_KMH', '700.0'))

GROUND_LEG_OVERHEAD_MIN = (10, 25)
AIR_LEG_OVERHEAD_MIN = (60, 150)
CUSTOMS_DWELL_MIN = (90, 600)            # minutes – used for uniform distribution
PICKUP_WINDOW_MIN = (20, 90)
LAST_MILE_OVERHEAD_MIN = (20, 60)

AIR_MODE_DISTANCE_THRESHOLD_KM = float(os.getenv('SIM_AIR_THRESHOLD_KM', '600.0'))
MIN_LEG_DISTANCE_FOR_CHECKPOINT_KM = float(os.getenv('SIM_MIN_LEG_CHECKPOINT_KM', '5.0'))

TICK_SECONDS_MIN = float(os.getenv('SIM_TICK_SECONDS_MIN', '8'))
TICK_SECONDS_MAX = float(os.getenv('SIM_TICK_SECONDS_MAX', '25'))
MIN_TICK_SECONDS = float(os.getenv('SIM_MIN_TICK_SECONDS', '3'))
MAX_TICK_SECONDS = float(os.getenv('SIM_MAX_TICK_SECONDS', '90'))
CHECKPOINT_MIN_GAP_SECONDS = int(os.getenv('SIM_CHECKPOINT_MIN_GAP_SECONDS', '120'))

SIM_DEFAULT_DAYS = float(os.getenv('SIM_DEFAULT_DAYS', '10.0'))

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
sim_logger = logging.getLogger('simulator_engine')
sim_logger.setLevel(logging.DEBUG)  # Controlled by root logger config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def country_of(place: str) -> str:
    if not place or ',' not in place:
        return ''
    return place.rsplit(',', 1)[-1].strip().upper()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    h = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 6371.0 * 2 * atan2(sqrt(h), sqrt(1 - h))


def _rand_span(bounds: Tuple[float, float]) -> timedelta:
    lo, hi = bounds
    return timedelta(minutes=random.uniform(lo, hi))


def _rand_customs_dwell() -> timedelta:
    """
    Return a realistic customs delay following a log‑normal distribution.
    Mean ~ 4 hours, sigma 1.2 → values between ~1h and 24h.
    """
    minutes = random.lognormvariate(4.0, 1.2)
    return timedelta(minutes=minutes)


class Stage(str, Enum):
    PICKUP = "pickup"
    TRANSIT = "transit"
    CUSTOMS = "customs"
    DELIVERY = "delivery"
    DELIVERED = "delivered"
    EXCEPTION = "exception"


class EventKind(str, Enum):
    PICKUP_REQUEST = "pickup_request"
    PICKUP_COMPLETE = "pickup_complete"
    LEG_DEPART = "leg_depart"
    LEG_ARRIVE = "leg_arrive"
    CUSTOMS_HELD = "customs_held"
    CUSTOMS_CLEARED = "customs_cleared"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERY_ATTEMPT_FAILED = "delivery_attempt_failed"
    DELIVERED = "delivered"
    RETURNED = "returned"


@dataclass
class Leg:
    from_place: str
    to_place: str
    from_coords: Dict[str, float]
    to_coords: Dict[str, float]
    mode: str
    distance_km: float
    travel_duration: timedelta
    is_international: bool
    customs_dwell: timedelta
    start_offset: timedelta = field(default_factory=lambda: timedelta(0))
    events_emitted: set = field(default_factory=set)

    @property
    def travel_end_offset(self) -> timedelta:
        return self.start_offset + self.travel_duration

    @property
    def end_offset(self) -> timedelta:
        return self.travel_end_offset + self.customs_dwell

    def interpolated_position(self, frac: float) -> Tuple[float, float]:
        frac = max(0.0, min(1.0, frac))
        lat = self.from_coords['lat'] + (self.to_coords['lat'] - self.from_coords['lat']) * frac
        lon = self.from_coords['lon'] + (self.to_coords['lon'] - self.from_coords['lon']) * frac
        return lat, lon


@dataclass
class TransitPlan:
    legs: List[Leg]
    total_duration: timedelta
    max_delivery_attempts: int
    pickup_pad: timedelta
    last_mile_pad: timedelta
    created_at: datetime = field(default_factory=datetime.now)

    def locate(self, elapsed: timedelta) -> Tuple[Optional[Leg], float, bool]:
        if not self.legs:
            return None, 0.0, False
        if elapsed <= self.legs[0].start_offset:
            return self.legs[0], 0.0, False
        for leg in self.legs:
            if elapsed < leg.travel_end_offset:
                span = leg.travel_duration.total_seconds() or 1.0
                frac = (elapsed - leg.start_offset).total_seconds() / span
                return leg, max(0.0, min(1.0, frac)), False
            if elapsed < leg.end_offset:
                return leg, 1.0, True
        return self.legs[-1], 1.0, False

    def progress_fraction(self, elapsed: timedelta) -> float:
        total = self.total_duration.total_seconds() or 1.0
        return max(0.0, min(1.0, elapsed.total_seconds() / total))


def build_transit_plan(
    route_places: List[str],
    coords_lookup: Callable[[str], Optional[Dict[str, float]]],
    mode_hint: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> TransitPlan:
    r = rng or random
    places = [p for p in route_places if p]
    legs: List[Leg] = []
    offset = timedelta(0)

    if len(places) < 2:
        only = places[0] if places else "Unknown"
        c = coords_lookup(only) or {'lat': 0.0, 'lon': 0.0}
        leg = Leg(only, only, c, c, 'ground', 0.0, timedelta(minutes=30), False, timedelta(0))
        leg.start_offset = timedelta(0)
        return TransitPlan([leg], leg.end_offset, max_delivery_attempts=1,
                           pickup_pad=timedelta(0), last_mile_pad=timedelta(0))

    for a, b in zip(places, places[1:]):
        ca = coords_lookup(a) or {'lat': 0.0, 'lon': 0.0}
        cb = coords_lookup(b) or {'lat': 0.0, 'lon': 0.0}
        distance_km = haversine_km(ca['lat'], ca['lon'], cb['lat'], cb['lon'])

        mode = mode_hint or ('air' if distance_km > AIR_MODE_DISTANCE_THRESHOLD_KM else 'ground')
        avg_speed = AIR_AVG_SPEED_KMH if mode == 'air' else GROUND_AVG_SPEED_KMH
        travel = timedelta(hours=distance_km / avg_speed) if avg_speed else timedelta(minutes=30)
        overhead = _rand_span(AIR_LEG_OVERHEAD_MIN if mode == 'air' else GROUND_LEG_OVERHEAD_MIN)
        travel_duration = travel + overhead

        international = bool(country_of(a)) and bool(country_of(b)) and country_of(a) != country_of(b)
        # Use log‑normal customs delay for more realism
        customs_dwell = _rand_customs_dwell() if (international and mode == 'air') else timedelta(0)

        leg = Leg(a, b, ca, cb, mode, distance_km, travel_duration, international, customs_dwell)
        leg.start_offset = offset
        legs.append(leg)
        offset = leg.end_offset

    pickup_pad = _rand_span(PICKUP_WINDOW_MIN)
    last_mile_pad = _rand_span(LAST_MILE_OVERHEAD_MIN)
    for leg in legs:
        leg.start_offset += pickup_pad

    total = offset + pickup_pad + last_mile_pad
    max_attempts = 3 if r.random() < 0.15 else 1
    return TransitPlan(legs, total, max_attempts, pickup_pad, last_mile_pad)


# ---------------------------------------------------------------------------
# Checkpoint generation (kept separate)
# ---------------------------------------------------------------------------
_GROUND_DEPART = [
    "Departed {a} facility by road",
    "Left {a} distribution center en route to {b}",
    "Collected from {a} sort facility for ground transfer",
]
_GROUND_ARRIVE = [
    "Arrived at {b} sort facility",
    "Processed at {b} distribution center",
    "Received at {b} facility",
]
_AIR_DEPART = [
    "Departed {a} on scheduled flight to {b}",
    "Loaded for air transfer from {a}",
    "Left {a} international hub by air",
]
_AIR_ARRIVE = [
    "Arrived {b} - cleared for onward processing",
    "Landed at {b}, transferred to sort facility",
    "Received at {b} air hub",
]
_CUSTOMS_HELD = [
    "Held at {b} for customs clearance",
    "Customs clearance in progress at {b}",
    "Import documentation under review at {b}",
]
_CUSTOMS_CLEARED = [
    "Customs clearance completed at {b}",
    "Released from customs at {b}",
]
_PICKUP_REQUEST = [
    "Pickup request received from shipper",
    "Courier assigned for pickup",
    "Shipment information received from shipper",
]
_PICKUP_COMPLETE = [
    "Package collected from shipper",
    "Picked up and scanned at origin",
]
_OUT_FOR_DELIVERY = [
    "Out for delivery to recipient address",
    "With delivery courier for final delivery",
    "Loaded onto delivery vehicle for final mile",
]
_DELIVERY_FAIL_REASONS = [
    ("Recipient not available", 0.45),
    ("Business closed at time of delivery", 0.2),
    ("Access issue at delivery address", 0.15),
    ("Delivery rescheduled at recipient's request", 0.12),
    ("Address requires additional information", 0.08),
]
_DELIVERED_TEMPLATES = [
    "Delivered successfully - Signed by: {pod}",
    "Delivered to recipient - {pod}",
]


class CheckpointGenerator:
    def __init__(self, tracking_number: str, rng: Optional[random.Random] = None):
        self.tn = tracking_number
        self.rng = rng or random

    def _line(self, when: datetime, city: str, event: str) -> str:
        facility_code = f"DHL{self.rng.randint(100, 999)}"
        ref = f"{facility_code}-{self.tn[-4:]}"
        return f"{when:%Y-%m-%d %H:%M} - {city} - {event} [Ref: {ref}]"

    def pickup_request(self, when: datetime, origin: str) -> str:
        return self._line(when, origin, self.rng.choice(_PICKUP_REQUEST))

    def pickup_complete(self, when: datetime, origin: str) -> str:
        return self._line(when, origin, self.rng.choice(_PICKUP_COMPLETE))

    def leg_depart(self, when: datetime, leg: Leg) -> str:
        templates = _AIR_DEPART if leg.mode == 'air' else _GROUND_DEPART
        text = self.rng.choice(templates).format(a=leg.from_place, b=leg.to_place)
        return self._line(when, leg.from_place, text)

    def leg_arrive(self, when: datetime, leg: Leg) -> str:
        templates = _AIR_ARRIVE if leg.mode == 'air' else _GROUND_ARRIVE
        text = self.rng.choice(templates).format(a=leg.from_place, b=leg.to_place)
        return self._line(when, leg.to_place, text)

    def customs_held(self, when: datetime, leg: Leg) -> str:
        text = self.rng.choice(_CUSTOMS_HELD).format(a=leg.from_place, b=leg.to_place)
        return self._line(when, leg.to_place, text)

    def customs_cleared(self, when: datetime, leg: Leg) -> str:
        text = self.rng.choice(_CUSTOMS_CLEARED).format(a=leg.from_place, b=leg.to_place)
        return self._line(when, leg.to_place, text)

    def out_for_delivery(self, when: datetime, delivery_address: str) -> str:
        return self._line(when, delivery_address, self.rng.choice(_OUT_FOR_DELIVERY))

    def delivery_attempt_failed(self, when: datetime, delivery_address: str) -> Tuple[str, str]:
        reasons, weights = zip(*_DELIVERY_FAIL_REASONS)
        reason = self.rng.choices(reasons, weights=weights)[0]
        line = self._line(when, delivery_address, f"Delivery attempted - {reason}")
        return line, reason

    def delivered(self, when: datetime, delivery_address: str, pod: str) -> str:
        text = self.rng.choice(_DELIVERED_TEMPLATES).format(pod=pod)
        return self._line(when, delivery_address, text)

    def returned(self, when: datetime, origin: str) -> str:
        return self._line(when, origin, "Returned to shipper after failed delivery attempts")


# ---------------------------------------------------------------------------
# High-level runner with robust hooks
# ---------------------------------------------------------------------------
@dataclass
class RunnerHooks:
    get_live_shipment: Callable[[], Any]
    save_shipment: Callable[[str, str], None]
    get_flag: Callable[[str, str], str]
    set_flag: Callable[[str, str], None]
    resolve_location: Callable[[str], Tuple[str, Optional[Dict[str, float]]]]
    build_route_hubs: Callable[[Dict[str, float], Dict[str, float], float], List[str]]
    on_position_update: Callable[[float, str, float, float, str], None]
    on_checkpoint_added: Callable[[str], None]
    on_status_changed: Callable[[str], None]
    broadcast: Callable[[], None]
    sleep: Callable[[float], None]
    now: Callable[[], datetime] = field(default_factory=lambda: datetime.now)
    generate_pod: Callable[[], str] = field(default=lambda: "SIGNATURE ON FILE")
    # Optional: if provided, called on every loop iteration to check lock validity
    check_lock: Optional[Callable[[], bool]] = None
    # Optional: called when simulation finishes (including exceptions)
    on_finished: Optional[Callable[[], None]] = None


class SimulationRunner:
    def __init__(self, tracking_number: str, hooks: RunnerHooks, sim_days_cap: float = None):
        self.tn = tracking_number
        self.hooks = hooks
        self.sim_days_cap = max(1.0, min(365.0, sim_days_cap if sim_days_cap is not None else SIM_DEFAULT_DAYS))
        self.rng = random.Random()
        self.checkpoints_generator = CheckpointGenerator(tracking_number, self.rng)
        self.start_monotonic: Optional[float] = None
        self.deadline_seconds: Optional[float] = None

    def _normalize_checkpoint_key(self, cp: str) -> str:
        """Return a unique key for checkpoint deduplication: location + event text."""
        parts = cp.split(" - ", 2)
        if len(parts) >= 3:
            location = parts[1].strip()
            event = re.sub(r'\s*\[Ref:\s*[^\]]+\]', '', parts[2]).strip()
            return f"{location}:{event}"
        return cp

    def _append_checkpoint_if_new(self, checkpoints: List[str], line: str) -> bool:
        """Append line only if its key differs from the last checkpoint's key."""
        if checkpoints:
            last = checkpoints[-1]
            if self._normalize_checkpoint_key(last) == self._normalize_checkpoint_key(line):
                return False
        checkpoints.append(line)
        return True

    def _build_plan(self, origin: str, destination: str) -> TransitPlan:
        origin_norm, origin_coords = self.hooks.resolve_location(origin)
        dest_norm, dest_coords = self.hooks.resolve_location(destination)
        distance_km = (
            haversine_km(origin_coords['lat'], origin_coords['lon'], dest_coords['lat'], dest_coords['lon'])
            if origin_coords and dest_coords else 1000.0
        )
        hubs = self.hooks.build_route_hubs(origin_coords, dest_coords, distance_km) if (origin_coords and dest_coords) else []
        route_places = [origin_norm] + hubs + [dest_norm]

        def lookup(place: str) -> Optional[Dict[str, float]]:
            if place == origin_norm:
                return origin_coords
            if place == dest_norm:
                return dest_coords
            _, c = self.hooks.resolve_location(place)
            return c

        plan = build_transit_plan(route_places, lookup, rng=self.rng)
        return plan

    def run(self) -> None:
        sim_logger.info(f"🚀 Starting simulation for {self.tn}")

        shipment = self.hooks.get_live_shipment()
        if not shipment:
            sim_logger.warning(f"Shipment {self.tn} not found – aborting")
            self._finish()
            return

        origin = shipment.origin_location or "Lagos, NG"
        destination = shipment.delivery_location
        plan = self._build_plan(origin, destination)

        checkpoints: List[str] = (shipment.checkpoints or "").split(";") if shipment.checkpoints else []
        checkpoints = [c for c in checkpoints if c]

        current_status = shipment.status
        delivery_attempts = 0
        stage = Stage.PICKUP
        start_wall = self.hooks.now()
        start_monotonic = time.monotonic()
        self.start_monotonic = start_monotonic
        self.deadline_seconds = self.sim_days_cap * 86400.0

        pickup_request_logged = False
        pickup_complete_logged = False
        # FIX: use simulated elapsed time for checkpoint gap, not wall-clock datetime
        last_checkpoint_elapsed: timedelta = timedelta(0)

        # Recover state from existing checkpoints
        for cp in checkpoints:
            key = self._normalize_checkpoint_key(cp)
            if "Pickup request" in key or "Shipment information" in key:
                pickup_request_logged = True
            if "Package collected" in key or "Picked up" in key:
                pickup_complete_logged = True

        sim_logger.debug(f"Sim days cap: {self.sim_days_cap} days → {self.deadline_seconds:.0f}s")

        while (time.monotonic() - start_monotonic) < self.deadline_seconds:
            # Optional lock check
            if self.hooks.check_lock and not self.hooks.check_lock():
                sim_logger.warning(f"Lock lost for {self.tn}, aborting simulation")
                self._finish()
                return

            live = self.hooks.get_live_shipment()
            if not live:
                sim_logger.warning(f"Shipment {self.tn} disappeared – aborting")
                self._finish()
                return

            if live.status == "On_Hold":
                self.hooks.sleep(30)
                continue

            if live.delivery_location != destination or live.origin_location != origin:
                origin = live.origin_location or "Lagos, NG"
                destination = live.delivery_location
                plan = self._build_plan(origin, destination)
                start_wall = self.hooks.now()
                start_monotonic = time.monotonic()
                self.start_monotonic = start_monotonic
                self.deadline_seconds = self.sim_days_cap * 86400.0
                sim_logger.debug(f"Plan rebuilt for {self.tn} after location change")

            if live.status != current_status:
                current_status = live.status

            if live.checkpoints and live.checkpoints != ";".join(checkpoints):
                checkpoints = (live.checkpoints or "").split(";")
                checkpoints = [c for c in checkpoints if c]

            if self.hooks.get_flag("paused_simulations", "false") == "true":
                self.hooks.sleep(10)
                continue

            speed_multiplier = self._read_speed_multiplier()
            elapsed_wall = self.hooks.now() - start_wall
            elapsed_sim = elapsed_wall * speed_multiplier

            leg, frac, in_customs = plan.locate(elapsed_sim)
            progress = plan.progress_fraction(elapsed_sim)
            now = self.hooks.now()

            # --- pickup stage ---
            if leg is plan.legs[0] and elapsed_sim < plan.pickup_pad:
                stage = Stage.PICKUP
                if not pickup_request_logged:
                    line = self.checkpoints_generator.pickup_request(now, origin)
                    if self._append_checkpoint_if_new(checkpoints, line):
                        self.hooks.on_checkpoint_added(line)
                        pickup_request_logged = True
                        last_checkpoint_elapsed = elapsed_sim
                        self._commit(checkpoints, "Pending", stage, progress, origin,
                                     plan.legs[0].from_coords['lat'], plan.legs[0].from_coords['lon'])
                elif elapsed_sim > plan.pickup_pad * 0.6 and not pickup_complete_logged:
                    line = self.checkpoints_generator.pickup_complete(now, origin)
                    if self._append_checkpoint_if_new(checkpoints, line):
                        self.hooks.on_checkpoint_added(line)
                        pickup_complete_logged = True
                        current_status = "In_Transit"
                        last_checkpoint_elapsed = elapsed_sim
                        self._commit(checkpoints, "In_Transit", stage, progress, origin,
                                     plan.legs[0].from_coords['lat'], plan.legs[0].from_coords['lon'])
                else:
                    self.hooks.on_position_update(progress, origin,
                                                  plan.legs[0].from_coords['lat'], plan.legs[0].from_coords['lon'],
                                                  stage.value)

            # --- transit / customs ---
            elif leg is not None and (leg is not plan.legs[-1] or (leg is plan.legs[-1] and elapsed_sim < leg.end_offset)):
                if in_customs:
                    stage = Stage.CUSTOMS
                    if 'held' not in leg.events_emitted:
                        line = self.checkpoints_generator.customs_held(now, leg)
                        if self._append_checkpoint_if_new(checkpoints, line):
                            self.hooks.on_checkpoint_added(line)
                            leg.events_emitted.add('held')
                            current_status = "Customs_Clearance"
                            last_checkpoint_elapsed = elapsed_sim
                            lat, lon = leg.to_coords['lat'], leg.to_coords['lon']
                            self._commit(checkpoints, "Customs_Clearance", stage, progress, leg.to_place, lat, lon)
                else:
                    stage = Stage.TRANSIT
                    lat, lon = leg.interpolated_position(frac)
                    if 'depart' not in leg.events_emitted and frac > 0.02:
                        line = self.checkpoints_generator.leg_depart(now, leg)
                        if self._append_checkpoint_if_new(checkpoints, line):
                            self.hooks.on_checkpoint_added(line)
                            leg.events_emitted.add('depart')
                            current_status = "In_Transit"
                            last_checkpoint_elapsed = elapsed_sim
                            self._commit(checkpoints, "In_Transit", stage, progress, leg.from_place, lat, lon)
                    elif ('customs_cleared' not in leg.events_emitted and leg.customs_dwell > timedelta(0)
                          and 'held' in leg.events_emitted and frac >= 0.999):
                        line = self.checkpoints_generator.customs_cleared(now, leg)
                        if self._append_checkpoint_if_new(checkpoints, line):
                            self.hooks.on_checkpoint_added(line)
                            leg.events_emitted.add('customs_cleared')
                            current_status = "In_Transit"
                            last_checkpoint_elapsed = elapsed_sim
                            self._commit(checkpoints, "In_Transit", stage, progress, leg.to_place, lat, lon)
                    elif 'arrive' not in leg.events_emitted and frac >= 0.999:
                        line = self.checkpoints_generator.leg_arrive(now, leg)
                        if self._append_checkpoint_if_new(checkpoints, line):
                            self.hooks.on_checkpoint_added(line)
                            leg.events_emitted.add('arrive')
                            current_status = "In_Transit"
                            last_checkpoint_elapsed = elapsed_sim
                            self._commit(checkpoints, "In_Transit", stage, progress, leg.to_place, lat, lon)
                    else:
                        # FIX: use simulated elapsed time for checkpoint gap
                        should_log_midpoint = (
                            leg.distance_km >= MIN_LEG_DISTANCE_FOR_CHECKPOINT_KM
                            and 0.15 < frac < 0.85
                            and (elapsed_sim - last_checkpoint_elapsed).total_seconds() >= CHECKPOINT_MIN_GAP_SECONDS
                            and self.rng.random() < 0.05
                        )
                        if should_log_midpoint:
                            line = self.checkpoints_generator.leg_depart(now, leg)
                            if self._append_checkpoint_if_new(checkpoints, line):
                                self.hooks.on_checkpoint_added(line)
                                last_checkpoint_elapsed = elapsed_sim
                                self._commit(checkpoints, "In_Transit", stage, progress, leg.from_place, lat, lon)
                        else:
                            self.hooks.on_position_update(progress, leg.from_place if frac < 0.5 else leg.to_place,
                                                           lat, lon, stage.value)

            # --- last‑mile delivery ---
            else:
                last_leg = plan.legs[-1]
                stage = Stage.DELIVERY
                lat, lon = last_leg.to_coords['lat'], last_leg.to_coords['lon']
                if 'out_for_delivery' not in last_leg.events_emitted:
                    line = self.checkpoints_generator.out_for_delivery(now, destination)
                    if self._append_checkpoint_if_new(checkpoints, line):
                        self.hooks.on_checkpoint_added(line)
                        last_leg.events_emitted.add('out_for_delivery')
                        current_status = "Out_for_Delivery"
                        last_checkpoint_elapsed = elapsed_sim
                        self._commit(checkpoints, "Out_for_Delivery", stage, progress, destination, lat, lon)
                else:
                    time_since_ofd = elapsed_sim - (last_leg.end_offset + plan.last_mile_pad)
                    if time_since_ofd >= timedelta(0):
                        if delivery_attempts < plan.max_delivery_attempts - 1 and self.rng.random() < 0.15:
                            line, reason = self.checkpoints_generator.delivery_attempt_failed(now, destination)
                            if self._append_checkpoint_if_new(checkpoints, line):
                                self.hooks.on_checkpoint_added(line)
                                delivery_attempts += 1
                                current_status = "Exception"
                                last_checkpoint_elapsed = elapsed_sim
                                self._commit(checkpoints, "Exception", Stage.EXCEPTION, progress, destination, lat, lon)
                                self.hooks.sleep(self._scaled_sleep(20, 60, speed_multiplier))
                                continue
                        else:
                            pod = self.hooks.generate_pod()
                            line = self.checkpoints_generator.delivered(now, destination, pod)
                            if self._append_checkpoint_if_new(checkpoints, line):
                                self.hooks.on_checkpoint_added(line)
                                current_status = "Delivered"
                                self._commit(checkpoints, "Delivered", Stage.DELIVERED, 1.0, destination, lat, lon)
                                self.hooks.broadcast()
                                self._finish()
                                return
                            else:
                                self.hooks.broadcast()
                                self._finish()
                                return

            self.hooks.broadcast()
            tick = self._scaled_sleep(TICK_SECONDS_MIN, TICK_SECONDS_MAX, speed_multiplier)
            self.hooks.sleep(tick)

        # sim_days cap reached without delivering
        live = self.hooks.get_live_shipment()
        if live and live.status not in ("Delivered", "Returned"):
            final_status = "Delivered" if delivery_attempts < plan.max_delivery_attempts else "Exception"
            line = self.checkpoints_generator.delivered(
                self.hooks.now(), destination, self.hooks.generate_pod()
            ) if final_status == "Delivered" else self.checkpoints_generator.returned(self.hooks.now(), origin)
            if self._append_checkpoint_if_new(checkpoints, line):
                self.hooks.on_checkpoint_added(line)
            self._commit(checkpoints, final_status,
                         Stage.DELIVERED if final_status == "Delivered" else Stage.EXCEPTION,
                         1.0, destination, plan.legs[-1].to_coords['lat'], plan.legs[-1].to_coords['lon'])
            self.hooks.broadcast()

        sim_logger.info(f"🏁 Simulation finished for {self.tn}")
        self._finish()

    # -- helpers --
    def _read_speed_multiplier(self) -> float:
        try:
            v = float(self.hooks.get_flag("sim_speed_multipliers", "1.0") or "1.0")
        except Exception:
            v = 1.0
        return max(0.1, min(10.0, v))

    def _scaled_sleep(self, lo: float, hi: float, speed_multiplier: float) -> float:
        base = self.rng.uniform(lo, hi) / max(speed_multiplier, 0.01)
        return max(MIN_TICK_SECONDS, min(MAX_TICK_SECONDS, base))

    def _commit(self, checkpoints: List[str], status: str, stage: Stage, progress: float,
                city: str, lat: float, lon: float) -> None:
        joined = ";".join(checkpoints[-50:])
        self.hooks.save_shipment(status, joined)
        self.hooks.set_flag("progress", str(round(progress, 4)))

        if self.hooks.get_flag("lock_stage", "false") != "true":
            self.hooks.set_flag("stage", stage.value)

        self.hooks.set_flag("current_location", city)
        self.hooks.set_flag("current_lat", str(lat))
        self.hooks.set_flag("current_lon", str(lon))
        self.hooks.on_status_changed(status)
        self.hooks.on_position_update(progress, city, lat, lon, stage.value)

    def _finish(self) -> None:
        """Call the optional on_finished hook if provided."""
        if self.hooks.on_finished:
            try:
                self.hooks.on_finished()
            except Exception as e:
                sim_logger.error(f"on_finished hook failed for {self.tn}: {e}")
