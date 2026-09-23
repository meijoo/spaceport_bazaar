import asyncio
import os
import uuid

import websockets

import bazaar_pb2 as bazaar

from collections import defaultdict


SERVER_URL = "wss://spaceport.edneo.com/ws"
SUBPROTOCOL = "bazaar.protobuf.v2"
STATION_ID = "P07"

# Keep enough resources for approximately this many future ticks.
SAFETY_TICKS = 3
MIN_TRADE_AMOUNT = 1
MAX_TRADE_AMOUNT = 3

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


def survival_score(inventory, demands):
    """
    Return the fewest estimated ticks remaining among resources.

    A higher score is better.
    """

    scores = []

    for resource in RESOURCES:
        required = demands[resource]

        if required > 0:
            scores.append(
                inventory[resource] / required
            )

    if not scores:
        return float("inf")

    return min(scores)


# --------------------------------------------------
# Printing functions
# --------------------------------------------------

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
    """Display the result of one of P07's commands."""

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

        return f"p07-{action}-{random_part}"

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

    def command_space_available(self, state):
        """Check total stored-result capacity."""

        used = len(state.request_results.items)
        maximum = (
            state.rules.max_request_records_per_station
        )

        if used >= maximum:
            print(
                "No command-result capacity remains:",
                used,
                "/",
                maximum,
            )
            return False

        return True

    def command_allowed_this_tick(self, state):
        """Check the per-tick command limit."""

        commands_this_tick = sum(
            1
            for result in state.request_results.items
            if result.processed_tick == state.tick
        )

        limit = (
            state.rules.new_commands_per_station_per_tick
        )

        if commands_this_tick >= limit:
            print(
                "Per-tick command limit reached:",
                commands_this_tick,
                "/",
                limit,
            )
            return False

        return True

    async def accept_helpful_offer(
        self,
        websocket,
        state,
    ):
        """
        Accept gifts and trades that improve P07's minimum
        estimated resource survival time.
        """

        inventory = bundle_values(
            state.self.inventory
        )
        demands = effective_upkeep(state)

        current_score = survival_score(
            inventory,
            demands,
        )

        best_offer = None
        best_score = current_score

        for offer in state.offers.items:
            if (
                offer.recipient_id != STATION_ID
                or offer.proposer_id == STATION_ID
                or offer.status
                != bazaar.OFFER_STATUS_OPEN
            ):
                continue

            received = bundle_values(offer.give)
            payment = bundle_values(offer.receive)

            # P07 must be able to pay everything requested.
            can_afford = all(
                inventory[resource]
                >= payment[resource]
                for resource in RESOURCES
            )

            if not can_afford:
                continue

            after_trade = {
                resource: (
                    inventory[resource]
                    - payment[resource]
                    + received[resource]
                )
                for resource in RESOURCES
            }

            total_received = sum(received.values())
            total_payment = sum(payment.values())

            # Gifts may contain between one and three
            # total resource units.
            is_gift = (
                MIN_TRADE_AMOUNT
                <= total_received
                <= MAX_TRADE_AMOUNT
                and total_payment == 0
            )

            # Normal trades must contain between one and
            # three total units on each side.
            is_small_trade = (
                MIN_TRADE_AMOUNT
                <= total_received
                <= MAX_TRADE_AMOUNT
                and MIN_TRADE_AMOUNT
                <= total_payment
                <= MAX_TRADE_AMOUNT
            )

            if not is_gift and not is_small_trade:
                print(
                    "BOT: Rejected oversized offer",
                    offer.offer_id,
                    f"(receive={total_received}, "
                    f"pay={total_payment})",
                )
                continue

            # Make sure the trade does not dangerously
            # reduce a resource P07 already owns.
            keeps_safe_reserve = True

            for resource in RESOURCES:
                reserve = (
                    demands[resource]
                    * SAFETY_TICKS
                )

                # If already below the desired reserve,
                # the trade must not make it even worse.
                minimum_allowed = min(
                    inventory[resource],
                    reserve,
                )

                if (
                    after_trade[resource]
                    < minimum_allowed
                ):
                    keeps_safe_reserve = False
                    break

            if not keeps_safe_reserve:
                print(
                    "BOT: Rejected unsafe offer",
                    offer.offer_id,
                    "because it reduces a protected resource",
                )
                continue

            new_score = survival_score(
                after_trade,
                demands,
            )

            # Accept small gifts automatically.
            # Other trades must improve survival.
            if is_gift or new_score > best_score:
                best_offer = offer
                best_score = new_score

        if best_offer is None:
            return False

        message = bazaar.ClientMessage()

        message.accept.type = (
            bazaar.ACCEPT_TYPE_ACCEPT
        )
        message.accept.protocol_version = "2.0"
        message.accept.run_id = state.run_id
        message.accept.request_id = (
            self.new_request_id("accept")
        )
        message.accept.body.offer_id = (
            best_offer.offer_id
        )

        if not message.IsInitialized():
            raise ValueError(
                message.FindInitializationErrors()
            )

        await websocket.send(
            message.SerializeToString()
        )

        print(
            "BOT: Accepted helpful offer",
            best_offer.offer_id,
        )

        return True

    def choose_resources(self, state):
        """
        Choose the resource P07 needs most and the safest
        resource to use as payment.
        """

        inventory = bundle_values(
            state.self.inventory
        )
        demands = effective_upkeep(state)

        def remaining_ticks(resource):
            required = demands[resource]

            if required == 0:
                return float("inf")

            return (
                inventory[resource] / required
            )

        non_specialty_resources = [
            resource
            for resource in RESOURCES
            if resource != state.self.specialty
        ]

        # Prefer seeking a non-specialty resource.
        needed_resource = min(
            non_specialty_resources,
            key=remaining_ticks,
        )

        possible_payments = []

        for resource in RESOURCES:
            if resource == needed_resource:
                continue

            reserve = (
                demands[resource] * SAFETY_TICKS
            )

            # Confirm that paying one unit leaves the reserve.
            if inventory[resource] - 1 >= reserve:
                possible_payments.append(resource)

        if not possible_payments:
            return needed_resource, None

        # Prefer paying with the station's specialty when safe.
        if state.self.specialty in possible_payments:
            payment_resource = state.self.specialty
        else:
            payment_resource = max(
                possible_payments,
                key=remaining_ticks,
            )

        return needed_resource, payment_resource

    async def make_helpful_offer(
        self,
        websocket,
        state,
        needed_resource,
        payment_resource,
    ):
        """
        Send a one-for-one offer to a station whose
        advertisement matches P07's needs.
        """

        if payment_resource is None:
            return False

        # Wait while P07 already has an open outgoing offer.
        for offer in state.offers.items:
            if (
                offer.proposer_id == STATION_ID
                and offer.status
                == bazaar.OFFER_STATUS_OPEN
            ):
                print(
                    "BOT: Waiting for an existing "
                    "outgoing offer."
                )
                return False

        ranked_advertisements = sorted(
            state.advertisements.items,
            key=lambda advertisement: (
                self.specialty_evidence_score(
                    advertisement.station_id,
                    needed_resource,
                )
            ),
            reverse=True,
        )

        for advertisement in ranked_advertisements:
            if advertisement.station_id == STATION_ID:
                continue

            if (
                advertisement.status
                != bazaar.PUBLICATION_STATUS_ACTIVE
            ):
                continue

            peer_sells_needed = (
                needed_resource
                in advertisement.selling.items
            )

            peer_wants_payment = (
                payment_resource
                in advertisement.seeking.items
            )

            if not (
                peer_sells_needed
                and peer_wants_payment
            ):
                continue

            trade_key = (
                state.tick,
                advertisement.station_id,
                payment_resource,
                needed_resource,
            )

            if trade_key in self.sent_offer_keys:
                continue

            self.sent_offer_keys.add(trade_key)

            message = bazaar.ClientMessage()

            message.offer.type = (
                bazaar.OFFER_COMMAND_TYPE_OFFER
            )
            message.offer.protocol_version = "2.0"
            message.offer.run_id = state.run_id
            message.offer.request_id = (
                self.new_request_id("offer")
            )

            body = message.offer.body
            body.recipient_id = (
                advertisement.station_id
            )

            body.give.water = 0
            body.give.food = 0
            body.give.components = 0

            body.receive.water = 0
            body.receive.food = 0
            body.receive.components = 0

            if payment_resource == bazaar.RESOURCE_WATER:
                body.give.water = 1
            elif payment_resource == bazaar.RESOURCE_FOOD:
                body.give.food = 1
            else:
                body.give.components = 1

            if needed_resource == bazaar.RESOURCE_WATER:
                body.receive.water = 1
            elif needed_resource == bazaar.RESOURCE_FOOD:
                body.receive.food = 1
            else:
                body.receive.components = 1

            maximum_ttl = (
                state.rules.max_offer_ttl_ticks
            )

            if maximum_ttl < 1:
                return False

            ttl = min(3, maximum_ttl)
            body.expires_tick = state.tick + ttl

            if not message.IsInitialized():
                raise ValueError(
                    message.FindInitializationErrors()
                )

            await websocket.send(
                message.SerializeToString()
            )

            print(
                "BOT: Sent offer to",
                advertisement.station_id,
                "— give 1",
                RESOURCE_NAMES[payment_resource],
                "for 1",
                RESOURCE_NAMES[needed_resource],
            )

            return True

        return False

    async def advertise_needs(
        self,
        websocket,
        state,
        needed_resource,
        payment_resource,
    ):
        """
        Advertise the resource P07 can safely sell and
        the resource P07 currently needs.
        """

        if payment_resource is None:
            print(
                "BOT: No resource can safely be traded."
            )
            return False

        # Do not replace an already-correct advertisement.
        for advertisement in state.advertisements.items:
            if (
                advertisement.station_id
                == STATION_ID
                and advertisement.status
                == bazaar.PUBLICATION_STATUS_ACTIVE
                and set(advertisement.selling.items)
                == {payment_resource}
                and set(advertisement.seeking.items)
                == {needed_resource}
            ):
                print(
                    "BOT: Current advertisement already "
                    "matches our needs."
                )
                return False

        maximum_ttl = (
            state.rules.max_publication_ttl_ticks
        )

        if maximum_ttl < 1:
            return False

        message = bazaar.ClientMessage()

        message.advertise.type = (
            bazaar.ADVERTISE_TYPE_ADVERTISE
        )
        message.advertise.protocol_version = "2.0"
        message.advertise.run_id = state.run_id
        message.advertise.request_id = (
            self.new_request_id("advertise")
        )

        message.advertise.body.selling.items.append(
            payment_resource
        )
        message.advertise.body.seeking.items.append(
            needed_resource
        )

        ttl = min(6, maximum_ttl)

        message.advertise.body.expires_tick = (
            state.tick + ttl
        )

        if not message.IsInitialized():
            raise ValueError(
                message.FindInitializationErrors()
            )

        await websocket.send(
            message.SerializeToString()
        )

        print(
            "BOT: Advertised selling",
            RESOURCE_NAMES[payment_resource],
            "and seeking",
            RESOURCE_NAMES[needed_resource],
        )

        return True

    async def act(self, websocket, state):
        """Take at most one useful action for this state."""

        market_changed = self.observe_market(state)

        if market_changed:
            self.print_market_estimates(state)

        if state.phase != bazaar.PHASE_RUNNING:
            print(
                "BOT: Run is not currently running."
            )
            return

        if state.self.failed_once:
            print(
                "BOT: P07 has failed and cannot recover."
            )
            return

        if not self.command_space_available(state):
            return

        if not self.command_allowed_this_tick(state):
            return

        # Priority 1: accept gifts or survival-improving offers.
        acted = await self.accept_helpful_offer(
            websocket,
            state,
        )

        if acted:
            return

        needed_resource, payment_resource = (
            self.choose_resources(state)
        )

        print(
            "BOT: Most needed resource:",
            RESOURCE_NAMES[needed_resource],
        )

        if payment_resource is not None:
            print(
                "BOT: Safest payment resource:",
                RESOURCE_NAMES[payment_resource],
            )

        # Priority 2: send an offer matching another
        # station's advertisement.
        acted = await self.make_helpful_offer(
            websocket,
            state,
            needed_resource,
            payment_resource,
        )

        if acted:
            return

        # Priority 3: advertise P07's current needs.
        await self.advertise_needs(
            websocket,
            state,
            needed_resource,
            payment_resource,
        )


# --------------------------------------------------
# Main WebSocket client
# --------------------------------------------------

async def main():
    token = os.environ.get("BAZAAR_TOKEN")

    if not token:
        raise ValueError(
            "BAZAAR_TOKEN is missing.\n"
            "Run: export BAZAAR_TOKEN='your P07 token'"
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
                    "The server did not mark P07 ready"
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

        print("P07 is connected and ready.")
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

            elif update_type == "protocol_error":
                print_protocol_error(
                    update.protocol_error
                )

                if update.protocol_error.close_session:
                    print(
                        "The server requested that "
                        "the session close."
                    )
                    break

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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(
            "\nDisconnected from the Bazaar server."
        )
    except websockets.ConnectionClosed as error:
        print(
            "\nWebSocket connection closed:",
            error,
        )