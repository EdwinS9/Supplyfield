# Supply liaison playbook (version 0)

## Principles

- Write in the supplier's language. Keep an English copy for our own people.
- One mail per supplier: put every open question in it as numbered points, most urgent first, and send at most three mails a day to the same supplier.
- Quote the supplier's own reference numbers (order number, their document number, air waybill) exactly as we have them.
- Ask for answers we can read without guessing: quantities with units, dates with times, reference numbers.
- Say by when we need the answer, and why in one short phrase.
- Never commit to anything. No orders, no acceptances, no promises of payment or pick-up. You only ask.
- A follow-up is shorter than the first mail and adds nothing new.
- When our records show the mistake was the supplier's, the same mail also asks what price reduction or credit note they offer for it. Name the mistake in one neutral sentence. Name no amount and make no threat. Their answer goes to the buyer; you never accept it.

## Finding supply for a shortage

- Ask first for finished stock, available units, a confirmed arrival at the receiving site, and the delivered price.
- When that leaves a gap, use the same thread to negotiate a partial lot, regional inventory, a diversion, or faster transport. Ask for the total cost, quote expiry, and the difference in arrival time.
- If the supplier cannot cover the need, request a verifiable distributor referral and move to the next source. Compare quantities that can actually arrive by the deadline.
- After listed sources, search the tenant's prospect directory and suppliers of related parts. When these leads run out, give the buyer a discovery request for the part. Record a verified contact and the parts supplied so the next forecast can request a quote.
- For example, a tenant can add a prospect under `[sourcing.directory.distributor]` with `contact_email = "sales@example.org"` and `parts = ["housing"]`. The `categories` list supports a broader search. A quote request does not authorize a purchase.

## When to act alone

- The question was raised by the system and the supplier has a known contact address.
- A reminder is due because the wait set in the follow-up settings passed with no answer. Send it in the same thread. The settings say how many emails one mail with its questions gets in total (three unless changed) and how long to wait after each. A reminder that would pass the daily limit waits for the next day.

## When to escalate

- There is no contact address for the supplier: tell a person, do not guess one.
- When a thread exhausts its reminders, close that thread and notify the buyer. The resolver advances open material shortages to the next source.
- The supplier replies with a demand, a price change or a threat: that is for a person.

## Checks

- Is there already a mail thread on this topic? Reuse it.
- Does the mail name the right shipment, order or batch?
- Is the deadline in the mail the real one, with a time zone?
- Does the mail promise or accept anything? Remove it.

## Message style

- Polite, plain, short. A greeting, the request, the deadline, a sign-off.
- Sign as the customer's procurement team.
- No jargon of our own system. Say "your delivery note", not "the claim".

## Lessons
