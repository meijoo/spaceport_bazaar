import asyncio
import os
import sys
import traceback
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

import websockets

import bazaar_pb2 as bazaar

from collections import defaultdict


SERVER_URL = "wss://spaceport.edneo.com/ws"
#SERVER_URL = "ws://127.0.0.1:3001/ws"
SUBPROTOCOL = "bazaar.protobuf.v2"
STATION_ID = "P07"

# Keep enough resources for approximately this many future ticks.
SAFETY_TICKS = 3
TARGET_TICKS = 8
MAX_TRADE_AMOUNT = 6

RESOURCE_NAMES = {
    bazaar.RESOURCE_WATER: "water",
    bazaar.RESOURCE_FOOD: "food",
    bazaar.RESOURCE_COMPONENTS: "components",
}

RESOURCES = [
    bazaar.RESOURCE_WATER,
    bazaar.RESOURCE_FOOD,
    bazaar.RESOURCE_COMPONENTS,
]


# --------------------------------------------------
# General helper functions
# --------------------------------------------------

def bundle_values(bundle):
    """Convert a Protobuf Bundle into a dictionary."""

    return {
        bazaar.RESOURCE_WATER: bundle.water,
        bazaar.RESOURCE_FOOD: bundle.food,
        bazaar.RESOURCE_COMPONENTS: bundle.components,
    }


def effective_upkeep(state):
    """
    Estimate how quickly resources are being depleted.

    last_production is subtracted from upkeep because produced
    resources can replace some or all of that tick's consumption.
    """

    upkeep = bundle_values(state.self.upkeep_per_tick)
    production = bundle_values(state.self.last_production)

    demands = {}

    for resource in RESOURCES:
        demands[resource] = max(
            0,
            upkeep[resource] - production[resource],
        )

    # At tick 0, production has not happened yet. We know the
    # specialty is the resource this station produces, so avoid
    # treating it as the most urgent resource on an exact tie.
    if state.tick == 0:
        demands[state.self.specialty] = 0

    return demands


# --------------------------------------------------
# Printing functions
# --------------------------------------------------

class TeeOutput:
    """Copy terminal output to a log file, flushing every write."""

    def __init__(self, terminal, log_file):
        self.terminal = terminal
        self.log_file = log_file

    def write(self, text):
        self.log_file.write(text)
        self.log_file.flush()
        self.terminal.write(text)
        self.terminal.flush()
        return len(text)

    def flush(self):
        self.log_file.flush()
        self.terminal.flush()

    def __getattr__(self, name):
        return getattr(self.terminal, name)


def print_state(state):
    """Display important information from a state snapshot."""

    inventory = bundle_values(state.self.inventory)
    upkeep = bundle_values(state.self.upkeep_per_tick)
    production = bundle_values(state.self.last_production)
    demands = effective_upkeep(state)

    print("\n========== STATE ==========")
    print("Run ID:", state.run_id)
    print("Station:", state.self_station_id)
    print("World version:", state.world_version)
    print("Snapshot:", state.snapshot_sequence)
    print("Tick:", state.tick)
    print("Phase:", bazaar.Phase.Name(state.phase))
    print("Health:", state.self.health)
    print(
        "Specialty:",
        bazaar.Resource.Name(state.self.specialty),
    )

    print(
        "Inventory:",
        f"water={inventory[bazaar.RESOURCE_WATER]},",
        f"food={inventory[bazaar.RESOURCE_FOOD]},",
        f"components={inventory[bazaar.RESOURCE_COMPONENTS]}",
    )

    print(
        "Upkeep:",
        f"water={upkeep[bazaar.RESOURCE_WATER]},",
        f"food={upkeep[bazaar.RESOURCE_FOOD]},",
        f"components={upkeep[bazaar.RESOURCE_COMPONENTS]}",
    )

    print(
        "Last production:",
        f"water={production[bazaar.RESOURCE_WATER]},",
        f"food={production[bazaar.RESOURCE_FOOD]},",
        f"components={production[bazaar.RESOURCE_COMPONENTS]}",
    )

    print(
        "Estimated net demand:",
        f"water={demands[bazaar.RESOURCE_WATER]},",
        f"food={demands[bazaar.RESOURCE_FOOD]},",
        f"components={demands[bazaar.RESOURCE_COMPONENTS]}",
    )

    print("\nAdvertisements:")

    active_advertisements = [
        advertisement
        for advertisement in state.advertisements.items
        if advertisement.status
        == bazaar.PUBLICATION_STATUS_ACTIVE
    ]

    if not active_advertisements:
        print("  None")

    for advertisement in active_advertisements:
        selling = [
            bazaar.Resource.Name(resource)
            for resource in advertisement.selling.items
        ]

        seeking = [
            bazaar.Resource.Name(resource)
            for resource in advertisement.seeking.items
        ]

        print(
            f"  {advertisement.advertisement_id}:",
            f"station={advertisement.station_id},",
            f"selling={selling},",
            f"seeking={seeking}",
        )

    print("\nOpen offers:")

    open_offers = [
        offer
        for offer in state.offers.items
        if offer.status == bazaar.OFFER_STATUS_OPEN
    ]

    if not open_offers:
        print("  None")

    for offer in open_offers:
        print(
            f"  {offer.offer_id}:",
            f"from={offer.proposer_id},",
            f"to={offer.recipient_id},",
            f"give=({offer.give.water}, "
            f"{offer.give.food}, "
            f"{offer.give.components}),",
            f"receive=({offer.receive.water}, "
            f"{offer.receive.food}, "
            f"{offer.receive.components})",
        )

    print(
        "\nTransactions:",
        len(state.transactions.items),
    )

    print(
        "Stored command results:",
        len(state.request_results.items),
        "/",
        state.rules.max_request_records_per_station,
    )

    print("===========================\n")


def print_result(result):
    """Display the result of one of our commands."""

    print("\n--- COMMAND RESULT ---")
    print("Request ID:", result.request_id)
    print("Success:", result.ok)
    print("Code:", bazaar.ResultCode.Name(result.code))

    if result.object_id.WhichOneof("kind") == "value":
        print("Object ID:", result.object_id.value)

    if result.transaction_id.WhichOneof("kind") == "value":
        print(
            "Transaction ID:",
            result.transaction_id.value,
        )

    print("----------------------\n")


def print_protocol_error(error):
    """Display a protocol-level error."""

    print("\n--- PROTOCOL ERROR ---")
    print("Code:", bazaar.ControlCode.Name(error.code))
    print("Close session:", error.close_session)

    if error.request_id.WhichOneof("kind") == "value":
        print("Request ID:", error.request_id.value)

    print("----------------------\n")


# --------------------------------------------------
# Survival trading bot
# --------------------------------------------------

class SurvivalBot:
    def __init__(self):
        self.sent_offer_keys = set()
        self.accept_attempts = set()
        self.peer_attempts = {}
        self.sent_commands = {}
        self.pending_request = None
        self.pending_version = None

        # Evidence that each station specializes in each resource.
        self.market_evidence = defaultdict(
            lambda: defaultdict(float)
        )

        # Prevent the same objects from being counted again
        # whenever they appear in another state snapshot.
        self.seen_advertisements = set()
        self.seen_offers = set()
        self.seen_transactions = set()

    def new_request_id(self, action):
        """
        Generate a unique request ID.

        UUIDs prevent request-ID conflicts after reconnecting.
        """

        random_part = uuid.uuid4().hex[:12]

        return f"{STATION_ID.lower()}-{action}-{random_part}"

    def add_evidence(
        self,
        station_id,
        resource,
        amount,
    ):
        if station_id == STATION_ID:
            return

        self.market_evidence[station_id][resource] += (
            amount
        )


    def observe_market(self, state):
        """
        Update estimates from newly observed advertisements,
        offers, and completed transactions.

        Returns True if new evidence was found.
        """

        changed = False

        # An advertisement selling a resource suggests that
        # the station may produce or have excess of it.
        for advertisement in state.advertisements.items:
            advertisement_id = (
                advertisement.advertisement_id
            )

            if (
                advertisement_id
                in self.seen_advertisements
            ):
                continue

            self.seen_advertisements.add(
                advertisement_id
            )
            changed = True

            for resource in advertisement.selling.items:
                self.add_evidence(
                    advertisement.station_id,
                    resource,
                    2,
                )

        # What the proposer gives is evidence of what that
        # station has available.
        for offer in state.offers.items:
            if offer.offer_id in self.seen_offers:
                continue

            self.seen_offers.add(offer.offer_id)
            changed = True

            given = bundle_values(offer.give)

            for resource in RESOURCES:
                amount = min(given[resource], 3)

                if amount > 0:
                    self.add_evidence(
                        offer.proposer_id,
                        resource,
                        amount,
                    )

        # A completed transaction provides stronger evidence
        # because resources actually changed hands.
        for transaction in state.transactions.items:
            transaction_id = (
                transaction.transaction_id
            )

            if (
                transaction_id
                in self.seen_transactions
            ):
                continue

            self.seen_transactions.add(transaction_id)
            changed = True

            proposer_payment = bundle_values(
                transaction.give
            )
            recipient_payment = bundle_values(
                transaction.receive
            )

            for resource in RESOURCES:
                proposer_amount = min(
                    proposer_payment[resource],
                    3,
                )
                recipient_amount = min(
                    recipient_payment[resource],
                    3,
                )

                if proposer_amount > 0:
                    self.add_evidence(
                        transaction.proposer_id,
                        resource,
                        proposer_amount * 2,
                    )

                if recipient_amount > 0:
                    self.add_evidence(
                        transaction.recipient_id,
                        resource,
                        recipient_amount * 2,
                    )

        return changed


    def specialty_estimate(self, station_id):
        """
        Return the leading specialty estimate, its score,
        and a confidence value.
        """

        evidence = self.market_evidence[station_id]

        if not evidence:
            return None, 0, 0

        ranked = sorted(
            RESOURCES,
            key=lambda resource: evidence[resource],
            reverse=True,
        )

        best_resource = ranked[0]
        best_score = evidence[best_resource]
        second_score = evidence[ranked[1]]

        total_score = sum(
            evidence[resource]
            for resource in RESOURCES
        )

        # Require multiple pieces of evidence and a clear lead.
        if best_score < 3 or best_score == second_score:
            return None, best_score, 0

        confidence = (
            best_score / total_score
            if total_score > 0
            else 0
        )

        return best_resource, best_score, confidence


    def specialty_evidence_score(
        self,
        station_id,
        resource,
    ):
        return self.market_evidence[
            station_id
        ][resource]


    def print_market_estimates(self, state):
        print("\n--- ESTIMATED SPECIALTIES ---")

        station_names = {
            entry.station_id: entry.display_name
            for entry in state.directory.items
        }

        found_estimate = False

        for station_id in sorted(station_names):
            if station_id == STATION_ID:
                continue

            resource, score, confidence = (
                self.specialty_estimate(station_id)
            )

            if resource is None:
                print(
                    f"{station_id} "
                    f"({station_names[station_id]}): "
                    "not enough evidence"
                )
                continue

            found_estimate = True

            print(
                f"{station_id} "
                f"({station_names[station_id]}): "
                f"probably {RESOURCE_NAMES[resource]} "
                f"(evidence={score:.0f}, "
                f"confidence={confidence:.0%})"
            )

        if not found_estimate:
            print(
                "No confident estimates yet. "
                "More market activity is needed."
            )

        print("-------------------------------\n")

    def outgoing(self, state):
        return [o for o in state.offers.items
                if o.proposer_id == state.self_station_id
                and o.status == bazaar.OFFER_STATUS_OPEN
                and o.expires_tick > state.tick]

    def available_inventory(self, state):
        inventory = bundle_values(state.self.inventory)
        # Offers don't reserve inventory on the server. Reserve locally for
        # every possible acceptance, without counting any promised receipts.
        for offer in self.outgoing(state):
            for resource, amount in bundle_values(offer.give).items():
                inventory[resource] -= amount
        return inventory

    def reserves(self, state):
        # Protect actual upkeep even during a specialty production surge.
        upkeep = bundle_values(state.self.upkeep_per_tick)
        return {r: upkeep[r] * SAFETY_TICKS for r in RESOURCES}

    def needs(self, state):
        inventory = bundle_values(state.self.inventory)
        upkeep = bundle_values(state.self.upkeep_per_tick)
        targets = {r: upkeep[r] * (SAFETY_TICKS if r == state.self.specialty
                                   else TARGET_TICKS) for r in RESOURCES}
        return sorted([r for r in RESOURCES if inventory[r] < targets[r]],
                      key=lambda r: inventory[r] / max(1, upkeep[r]))

    def benefit(self, inventory, state):
        # Improving either of two tied shortages is useful. Cap the score at
        # the target so a large specialty surplus cannot mask starvation.
        upkeep = bundle_values(state.self.upkeep_per_tick)
        return sum(min(TARGET_TICKS, inventory[r] / upkeep[r])
                   for r in RESOURCES if upkeep[r])

    async def send_command(self, websocket, state, action, body):
        message = bazaar.ClientMessage()
        command = getattr(message, action)
        command.type = {'accept': bazaar.ACCEPT_TYPE_ACCEPT,
                        'offer': bazaar.OFFER_COMMAND_TYPE_OFFER,
                        'advertise': bazaar.ADVERTISE_TYPE_ADVERTISE}[action]
        command.protocol_version = '2.0'
        command.run_id = state.run_id
        command.request_id = self.new_request_id(action)
        command.body.CopyFrom(body)
        data = message.SerializeToString()
        if len(data) > state.rules.max_command_bytes:
            return False
        await websocket.send(data)
        self.pending_request = command.request_id
        self.pending_version = None
        self.sent_commands[command.request_id] = state.tick
        return True

    def record_result(self, result):
        if result.request_id in self.sent_commands:
            self.sent_commands[result.request_id] = result.processed_tick
        if result.request_id == self.pending_request:
            self.pending_version = result.processed_version

    async def accept_helpful_offer(self, websocket, state):
        inventory = self.available_inventory(state)
        reserves = self.reserves(state)
        best, best_gain = None, 0
        for offer in state.offers.items:
            if (offer.recipient_id != state.self_station_id
                    or offer.proposer_id == state.self_station_id
                    or offer.status != bazaar.OFFER_STATUS_OPEN
                    or offer.expires_tick <= state.tick
                    or (state.tick, offer.offer_id) in self.accept_attempts):
                continue
            payment, received = bundle_values(offer.receive), bundle_values(offer.give)
            if any(payment[r] > max(0, inventory[r]) for r in RESOURCES):
                continue
            after = {r: inventory[r] - payment[r] + received[r] for r in RESOURCES}
            if any(after[r] < min(inventory[r], reserves[r]) for r in RESOURCES):
                continue
            gift = sum(payment.values()) == 0 and sum(received.values()) > 0
            gain = self.benefit(after, state) - self.benefit(inventory, state)
            if gift or gain > best_gain:
                best, best_gain = offer, gain
                if gift:
                    break
        if best is None:
            return False
        sent = await self.send_command(websocket, state, 'accept',
                                      bazaar.AcceptBody(offer_id=best.offer_id))
        if sent:
            self.accept_attempts.add((state.tick, best.offer_id))
            print('BOT: Accepting helpful offer', best.offer_id)
        return sent

    async def make_helpful_offer(self, websocket, state):
        outgoing = self.outgoing(state)
        if len(outgoing) >= min(4, state.rules.max_open_outgoing_offers):
            return False
        inventory = self.available_inventory(state)
        reserves = self.reserves(state)
        upkeep = bundle_values(state.self.upkeep_per_tick)
        actual = bundle_values(state.self.inventory)
        ttl = min(6, state.rules.max_offer_ttl_ticks)
        if ttl < 1:
            return False
        for needed in self.needs(state):
            # Permit two independent suppliers per resource. Unsettled offers
            # never count as inventory, but bound duplicate procurement.
            relevant = [o for o in outgoing if bundle_values(o.receive)[needed]]
            if len(relevant) >= 2:
                continue
            ads = sorted(state.advertisements.items, key=lambda a: (
                self.peer_attempts.get((a.station_id, needed), -1),
                -self.specialty_evidence_score(a.station_id, needed)))
            for ad in ads:
                if (ad.station_id == state.self_station_id
                        or ad.status != bazaar.PUBLICATION_STATUS_ACTIVE
                        or ad.expires_tick <= state.tick
                        or needed not in ad.selling.items
                        or any(o.recipient_id == ad.station_id for o in relevant)):
                    continue
                key = (state.tick, ad.station_id, needed)
                if key in self.sent_offer_keys:
                    continue
                payments = [r for r in RESOURCES if r != needed
                            and inventory[r] > reserves[r]
                            and (not ad.seeking.items or r in ad.seeking.items)]
                if not payments:
                    continue
                payment = max(payments, key=lambda r: (
                    r == state.self.specialty, inventory[r] - reserves[r]))
                # Spend abundant specialty stock more readily near shortage.
                urgent = actual[needed] <= 2 * upkeep[needed]
                ratio = 2 if urgent and payment == state.self.specialty else 1
                promised = sum(bundle_values(o.receive)[needed] for o in relevant)
                deficit = max(0, TARGET_TICKS * upkeep[needed] - actual[needed] - promised)
                amount = min(MAX_TRADE_AMOUNT, deficit,
                             (inventory[payment] - reserves[payment]) // ratio)
                if amount < 1:
                    continue
                body = bazaar.OfferBody(recipient_id=ad.station_id,
                                       expires_tick=state.tick + ttl)
                for r in RESOURCES:
                    setattr(body.give, RESOURCE_NAMES[r], amount * ratio if r == payment else 0)
                    setattr(body.receive, RESOURCE_NAMES[r], amount if r == needed else 0)
                if await self.send_command(websocket, state, 'offer', body):
                    self.sent_offer_keys.add(key)
                    self.peer_attempts[(ad.station_id, needed)] = state.tick
                    print(f'BOT: Offered {amount * ratio} {RESOURCE_NAMES[payment]} '
                          f'to {ad.station_id} for {amount} {RESOURCE_NAMES[needed]}')
                    return True
        return False

    async def advertise_needs(self, websocket, state):
        inventory = self.available_inventory(state)
        reserves = self.reserves(state)
        selling = {r for r in RESOURCES if inventory[r] > reserves[r]}
        seeking = set(self.needs(state))
        if not seeking:
            return False
        for ad in state.advertisements.items:
            if (ad.station_id == state.self_station_id
                    and ad.status == bazaar.PUBLICATION_STATUS_ACTIVE
                    and ad.expires_tick > state.tick + 1
                    and set(ad.selling.items) == selling
                    and set(ad.seeking.items) == seeking):
                return False
        ttl = min(12, state.rules.max_publication_ttl_ticks)
        if ttl < 1:
            return False
        body = bazaar.AdvertiseBody(expires_tick=state.tick + ttl)
        body.selling.items.extend(sorted(selling))
        body.seeking.items.extend(sorted(seeking))
        body.selling.SetInParent()
        body.seeking.SetInParent()
        sent = await self.send_command(websocket, state, 'advertise', body)
        if sent:
            print('BOT: Advertised needs:', ', '.join(RESOURCE_NAMES[r] for r in sorted(seeking)))
        return sent

    async def act(self, websocket, state):
        if self.observe_market(state):
            self.print_market_estimates(state)
        for result in state.request_results.items:
            self.record_result(result)
        if self.pending_request is not None:
            if self.pending_version is None or state.world_version < self.pending_version:
                return
            self.pending_request = None
            self.pending_version = None
        if state.phase != bazaar.PHASE_RUNNING or state.self.failed_once:
            return
        self.sent_offer_keys = {k for k in self.sent_offer_keys if k[0] == state.tick}
        self.accept_attempts = {k for k in self.accept_attempts if k[0] == state.tick}
        # Count local sends too: queued snapshots may omit command results.
        records = {r.request_id: r.processed_tick for r in state.request_results.items}
        records.update(self.sent_commands)
        if len(records) >= state.rules.max_request_records_per_station:
            return
        if sum(t == state.tick for t in records.values()) >= state.rules.new_commands_per_station_per_tick:
            return
        if await self.accept_helpful_offer(websocket, state):
            return
        if await self.make_helpful_offer(websocket, state):
            return
        await self.advertise_needs(websocket, state)


# --------------------------------------------------
# Main WebSocket client
# --------------------------------------------------

async def main():
    token = os.environ.get("BAZAAR_TOKEN")

    if not token:
        raise ValueError(
            "BAZAAR_TOKEN is missing.\n"
            f"Run: export BAZAAR_TOKEN='your {STATION_ID} token'"
        )

    bot = SurvivalBot()

    print("Connecting to the live Bazaar server...")

    async with websockets.connect(
        SERVER_URL,
        additional_headers={
            "Authorization": f"Bearer {token}"
        },
        subprotocols=[SUBPROTOCOL],
    ) as websocket:

        print("Connected to the live Bazaar server")
        print("Subprotocol:", websocket.subprotocol)

        if websocket.subprotocol != SUBPROTOCOL:
            raise RuntimeError(
                "The server did not confirm the expected "
                "subprotocol"
            )

        # Receive initial state.
        raw_message = await websocket.recv()

        if not isinstance(raw_message, bytes):
            raise ValueError(
                "Expected binary Protobuf data"
            )

        server_message = bazaar.ServerMessage()
        server_message.ParseFromString(raw_message)

        message_type = (
            server_message.WhichOneof("message")
        )

        if message_type != "state":
            raise ValueError(
                f"Expected initial state, received "
                f"{message_type}"
            )

        initial_state = server_message.state

        if initial_state.self_station_id != STATION_ID:
            raise ValueError(
                f"Expected {STATION_ID}, but server "
                f"identified us as "
                f"{initial_state.self_station_id}"
            )

        print_state(initial_state)

        run_id = initial_state.run_id

        # Confirm readiness.
        ready_message = bazaar.ClientMessage()

        ready_message.ready.type = (
            bazaar.READY_TYPE_READY
        )
        ready_message.ready.protocol_version = "2.0"
        ready_message.ready.run_id = run_id
        ready_message.ready.ready = True
        ready_message.ready.snapshot_sequence = (
            initial_state.snapshot_sequence
        )

        await websocket.send(
            ready_message.SerializeToString()
        )

        print("Sent readiness confirmation")

        # Receive readiness confirmation.
        raw_response = await websocket.recv()

        if not isinstance(raw_response, bytes):
            raise ValueError(
                "Expected binary Protobuf data"
            )

        response = bazaar.ServerMessage()
        response.ParseFromString(raw_response)

        response_type = (
            response.WhichOneof("message")
        )

        if response_type == "readiness":
            print(
                "Ready:",
                response.readiness.ready,
            )

            if not response.readiness.ready:
                raise RuntimeError(
                    f"The server did not mark {STATION_ID} ready"
                )

        elif response_type == "protocol_error":
            print_protocol_error(
                response.protocol_error
            )
            raise RuntimeError(
                "The server rejected readiness"
            )

        else:
            raise RuntimeError(
                f"Expected readiness confirmation, "
                f"received {response_type}"
            )

        print(f"{STATION_ID} is connected and ready.")
        print("The survival bot is active.")
        print("Press Control+C to disconnect.\n")

        # The bot may act immediately if the run is active.
        await bot.act(
            websocket,
            initial_state,
        )

        # Continue receiving server-pushed messages.
        async for raw_update in websocket:
            if not isinstance(raw_update, bytes):
                print(
                    "Ignored a non-binary message"
                )
                continue

            update = bazaar.ServerMessage()
            update.ParseFromString(raw_update)

            update_type = (
                update.WhichOneof("message")
            )

            print("Received:", update_type)

            if update_type == "state":
                current_state = update.state

                print_state(current_state)

                await bot.act(
                    websocket,
                    current_state,
                )

            elif update_type == "result":
                print_result(update.result)
                bot.record_result(update.result)
                # Request a fresh authoritative state even after a rejection.
                sync = bazaar.ClientMessage()
                sync.sync.type = bazaar.SYNC_TYPE_SYNC
                sync.sync.protocol_version = "2.0"
                sync.sync.run_id = run_id
                await websocket.send(sync.SerializeToString())

            elif update_type == "protocol_error":
                print_protocol_error(
                    update.protocol_error
                )

                # Stop issuing commands after a control error; preserve the
                # server message instead of blindly resubmitting.
                raise RuntimeError("Server rejected a command; see protocol error above")

            elif update_type == "readiness":
                print(
                    "Readiness:",
                    update.readiness.ready,
                )

            else:
                print(
                    "Received an unknown message."
                )


if __name__ == "__main__":
    log_directory = Path(__file__).resolve().parent / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    log_path = log_directory / f"live_client_{timestamp}.log"

    with log_path.open("x", encoding="utf-8") as log_file:
        with redirect_stdout(TeeOutput(sys.stdout, log_file)), \
                redirect_stderr(TeeOutput(sys.stderr, log_file)):
            print("Logging to:", log_path)
            try:
                asyncio.run(main())
            except KeyboardInterrupt:
                print("\nDisconnected from the Bazaar server.")
            except websockets.ConnectionClosed as error:
                print("\nWebSocket connection closed:", error)
            except Exception:
                # Print before restoring stderr so the log includes the traceback.
                traceback.print_exc()
                sys.exit(1)
