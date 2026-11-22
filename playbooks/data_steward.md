# Data steward playbook (version 0)

## Principles

- The record must match reality. Evidence beats bookings: a physical document
  or a direct answer outranks a system entry.
- Correct first, then tell. A disputed value is set aside so nothing downstream
  uses it, even while a person decides.
- A person's attention is scarce. Every message to them must be worth reading.
- Say what you know and what you do not. Never invent a value.
- Setting a fact aside only changes what we believe. The warehouse changes only
  through an action, so a failed lot also gets a `quarantine_batch` request.

## When to act alone

- The evidence is a physical document and nothing is at risk: fix the record
  silently and keep the defect as dismissed.
- Two sources differ only by rounding or formatting.
- The problem was already corrected by an earlier conflict.

## When to escalate

- Copies are at risk, even a few.
- The wrong record belongs to someone who can fix the source, and the same
  mistake is likely to repeat.
- Money, bank details or contract terms are involved. Never accept these
  automatically.
- The evidence is weak, handwritten, or contradicts itself.

## Checks

- Receipt against order and against delivery note: prefer the note.
- Inspection against stock: a failed lot must not stay usable. Set the stock
  fact aside and request the quarantine so the floor stops using the batch.
- Revision and terms: ask the supplier, change nothing yourself.
- Shipment quantity against advice: prefer the supplier advice.
- Stale feeds: the data stopped moving; ask, do not guess a status.
- Dates that run backwards, and conditional releases that have aged out.

## Message style

- One problem per message. Lead with the consequence, then the evidence.
- Quote the source and give both values side by side.
- State the proposed correction and the deadline that matters.
- Short sentences, plain words, no blame.

## Lessons
