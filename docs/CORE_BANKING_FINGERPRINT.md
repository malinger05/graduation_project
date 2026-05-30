# Core Banking — fingerprint integration

Implement these changes in the **`core-banking-system`** sibling repo so card activation can enroll or verify fingerprints before PIN setup.

## 1. Database / entity

Add to the account entity (or customer entity if accounts share biometrics):

```java
@Column(name = "fingerprint_slot_id")
private Integer fingerprintSlotId;  // null until first enroll
```

Migration (if not using `ddl-auto=create-drop`):

```sql
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS fingerprint_slot_id INTEGER;
```

## 2. Extend `POST /atm/prepare-own-pin`

Response must include:

| Field | Type | Meaning |
|-------|------|---------|
| `hadExistingCards` | boolean | Account already has at least one card with PIN set |
| `fingerprintSlotId` | integer or null | Sensor slot stored after first enroll |

Example — first card on account:

```json
{
  "cardId": 42,
  "maskedNumber": "**** 1234",
  "accountNumber": "DE00...",
  "hadExistingCards": false,
  "fingerprintSlotId": null
}
```

Example — additional card, fingerprint already enrolled:

```json
{
  "cardId": 43,
  "maskedNumber": "**** 5678",
  "accountNumber": "DE00...",
  "hadExistingCards": true,
  "fingerprintSlotId": 17
}
```

Logic sketch:

```java
boolean hadExistingCards = cardRepository.countActiveCardsWithPin(accountId) > 0;
// Or: count cards with non-null pin hash excluding the card being activated

Integer slot = account.getFingerprintSlotId();
// hadExistingCards true when another card on same account is already active
```

## 3. New endpoint — `POST /atm/register-fingerprint`

**Auth:** `X-Service-Token` (same as other `/atm/*` service routes).

**Request:**

```json
{
  "accountNumber": "DE89370400440532013000",
  "fingerprintSlotId": 17
}
```

**Rules:**

- Account must exist.
- `fingerprintSlotId` must be `>= 0`.
- Reject if account already has `fingerprintSlotId` set (409 — first enroll only).
- Set `account.fingerprintSlotId = fingerprintSlotId` and save.

**Response (200):**

```json
{
  "status": "ok",
  "message": "Fingerprint registered.",
  "fingerprintSlotId": 17
}
```

**Errors:**

- 404 — account not found
- 409 — fingerprint already registered for this account
- 400 — invalid slot id

## 4. Security configuration

Allow unauthenticated service access (mirror `set-own-pin`):

```java
.requestMatchers(HttpMethod.POST, "/atm/register-fingerprint").hasRole("SERVICE")
```

## 5. Optional DTOs (Spring)

```java
public record RegisterFingerprintRequest(
    String accountNumber,
    int fingerprintSlotId
) {}

public record PrepareOwnPinResponse(
    Long cardId,
    String maskedNumber,
    String accountNumber,
    boolean hadExistingCards,
    Integer fingerprintSlotId
) {}
```

## 6. Test checklist

1. New account, first card → `hadExistingCards=false`, `fingerprintSlotId=null`.
2. After ATM enroll + `register-fingerprint` → DB has slot id.
3. Second issued card → `hadExistingCards=true`, same `fingerprintSlotId`.
4. Duplicate `register-fingerprint` → 409.
5. Wrong account on register → 404.

## 7. Slot id alignment

The ATM (`graduation_project`) assigns slots with:

```python
abs(hash(account_number)) % 127
```

The value sent to `register-fingerprint` is the same id used on the sensor during enroll (`EnrollStart` param). No extra mapping is required in Core Banking — store the integer as returned.
