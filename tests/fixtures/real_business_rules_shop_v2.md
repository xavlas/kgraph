# Business Rules — shop

## Executive summary

Rules mined from `legacy/shop` (symlink to `samples/shop`: Spring Boot Java + Kotlin backend, Angular and React frontends, one Flyway migration; 34 files, ~587 lines). All backend and DB files were read in full. The frontends were searched for validation, role or status logic and contain none, so they only call the API. Citations are relative to `legacy/shop/`. `legacy/` was not modified.

**Headline finding:** the rules are mostly intentional and easy to state, but the order flow **cannot work end to end as written**. Order creation never links the customer (RULE-012 / DEFECT-01). The order-created event goes to the wrong topic with a payload the consumer cannot parse (RULE-016). Any `UPDATE` on `orders`, including cancel, would hit an audit trigger that writes to a table the migration never creates (RULE-015). Newly registered customers are probably created inactive (RULE-017). Treat everything below as *observed behaviour*, not confirmed business intent, and validate with a business SME before it goes into a target design. Items marked **Inference** come from framework behaviour I did not execute.

## Rules summary

| ID | Name | Category | Source | Confidence |
|----|------|----------|--------|------------|
| RULE-001 | Maximum single-order amount 10000 | Calculation | `backend/src/main/java/com/acme/shop/order/OrderService.java:14,40-42` | High |
| RULE-002 | Outstanding balance excludes cancelled orders | Calculation | `…/order/OrderRepository.java:13` | High |
| RULE-003 | Credit-limit check on new order | Calculation | `…/order/OrderService.java:43-46` | High |
| RULE-004 | Monetary precision NUMERIC(12,2) | Data | `db/migration/V1__init.sql:5,12` | High |
| RULE-005 | Stock reservation | Calculation | `backend/src/main/kotlin/com/acme/shop/inventory/InventoryService.kt:10-19` | High |
| RULE-006 | Order requires an existing, active customer | Validation | `…/order/OrderService.java:38`, `…/customer/CustomerService.java:17-23` | High |
| RULE-007 | Order request field validation | Validation | `…/order/CreateOrderRequest.java:9-19`, `OrderController.java:24` | High |
| RULE-008 | Order total must be positive | Validation | `…/order/Order.java:26-28` | Medium |
| RULE-009 | Order status limited to four values | Validation | `…/order/OrderStatus.java:3`, `V1__init.sql:14` | High |
| RULE-010 | Customer email format and uniqueness | Validation | `…/customer/Customer.java:13-14`, `V1__init.sql:3` | Medium |
| RULE-011 | Credit limit defaults to 5000, never negative | Validation | `V1__init.sql:5` | High |
| RULE-012 | New order starts as NEW | Lifecycle | `…/order/OrderService.java:48`, `Order.java:33-36` | High |
| RULE-013 | Shipped orders cannot be cancelled | Lifecycle | `…/order/OrderService.java:55-62` | High |
| RULE-014 | Only ORDER_MANAGER may cancel | Authorization | `…/order/OrderController.java:28-30` | High |
| RULE-015 | Order status changes are audited by trigger | Audit | `V1__init.sql:31-39` | Medium |
| RULE-016 | Order-created event drives stock reservation | Integration | `OrderService.java:51`, `…/inventory/OrderEventsListener.kt:9-12`, `application.yml:4-6` | High |
| RULE-017 | Customer registration accepts the raw payload | Validation | `…/customer/CustomerController.java:16-17`, `CustomerService.java:29-31` | Medium |
| RULE-018 | Order line quantity must be positive | Validation | `V1__init.sql:21`, `…/order/OrderLine.java:19-20` | High |
| RULE-019 | Stock available quantity never negative | Validation | `V1__init.sql:27` | High |

## Rule cards

### RULE-001 — Maximum single-order amount
- **Statement:** An order whose amount is greater than 10000 is rejected with "Order amount exceeds the maximum of 10000". Exactly 10000 is allowed.
- **Source:** `OrderService.java:14,40-42` (constant, hard-coded, not configurable).
- **Notes:** The limit is a bare `BigDecimal` literal and the message repeats the number. It is checked *after* the active-customer check and *before* the credit check. The default credit limit is 5000, so the 10000 cap only matters for customers whose limit was raised.

### RULE-002 — Outstanding balance excludes cancelled orders
- **Statement:** A customer's outstanding amount is the sum of `total_amount` over all their orders whose status is not `CANCELLED`, or 0 if there are none.
- **Source:** `OrderRepository.java:13` (native SQL).
- **Notes:** `PAID` and `SHIPPED` orders still count. Nothing in the code ever reduces the balance except cancelling. Whether "outstanding" should exclude paid orders is a **business question**.

### RULE-003 — Credit-limit check on new order
- **Statement:** The order is rejected with "Customer credit limit exceeded" if `outstanding + new amount > credit limit`. Equality is allowed.
- **Source:** `OrderService.java:43-46`; `CustomerService.java:25-27`.
- **Notes:** If the customer row is missing, the limit falls back to 0. This is unreachable in practice because RULE-006 runs first. The check and the insert are not locked, so two concurrent orders can both pass and together exceed the limit (race; **Inference** from the absence of locking).

### RULE-004 — Monetary precision
- **Statement:** Credit limit and order total are stored as `NUMERIC(12,2)`: up to 10 integer digits and 2 decimals.
- **Source:** `V1__init.sql:5,12`. Java side uses `BigDecimal`, with no explicit scale or rounding mode.

### RULE-005 — Stock reservation
- **Statement:** Reserving `qty` of a SKU requires `qty > 0`, a known SKU and `availableQty >= qty`. It then moves `qty` from `availableQty` to `reserved`.
- **Source:** `InventoryService.kt:10-19`.
- **Notes:** Failures raise `IllegalArgumentException` (non-positive qty) or `IllegalStateException` (unknown SKU, insufficient stock). Nothing releases a reservation, including on order cancellation (**gap**). Reads and writes are not atomic (no lock or `@Transactional`), so concurrent reservations can oversell, with the DB check in RULE-019 as the last backstop.

### RULE-006 — Order requires an existing, active customer
- **Statement:** Creating an order fails with "Customer not found" if the customer id does not exist, and with "Customer account is deactivated" if `active` is false.
- **Source:** `OrderService.java:38` (comment tag `BR-ORD-01`), `CustomerService.java:17-23`.
- **Notes:** No code path in this extract ever sets `active = false` other than raw registration (RULE-017).

### RULE-007 — Order request field validation
- **Statement:** `customerId` and `quantity` are required. `sku` is required, non-blank and 3–40 characters. `amount` has **no** constraint.
- **Source:** `CreateOrderRequest.java:9-19`; enforced by `@Valid` at `OrderController.java:24`.
- **Notes:** `quantity` has no minimum, so 0 or negative passes the API (RULE-018 only guards the DB table, which is never written). A missing `amount` causes a `NullPointerException` at `OrderService.java:40`, giving HTTP 500 rather than a validation error (DEFECT-02).

### RULE-008 — Order total must be positive
- **Statement:** `totalAmount` must be greater than 0.
- **Source:** `Order.java:26-28` (`@Positive`).
- **Notes:** Enforced by Bean Validation when the entity is persisted, so a zero or negative amount passes RULE-001/003 and fails afterwards with a `ConstraintViolationException`, not a `BusinessException`. **Inference** that the JPA provider validates on persist.

### RULE-009 — Order status limited to four values
- **Statement:** Allowed statuses are `NEW`, `PAID`, `SHIPPED`, `CANCELLED`, in the enum and in a DB `CHECK`.
- **Source:** `OrderStatus.java:3`, `V1__init.sql:14`.
- **Notes:** The only transition implemented is "anything except SHIPPED → CANCELLED" (RULE-013). `NEW → PAID → SHIPPED` has no code in this extract.

### RULE-010 — Customer email format and uniqueness
- **Statement:** Email must be well-formed when present, not null, and unique across customers.
- **Source:** `Customer.java:13-14` (`@Email`), `V1__init.sql:3` (`NOT NULL UNIQUE`, 200 chars).
- **Notes:** `@Email` accepts null, and the controller has no `@Valid`, so format is only checked on persist (**Inference**). `findByEmail` exists but is never used, so uniqueness surfaces as a DB constraint error.

### RULE-011 — Credit limit default and floor
- **Statement:** Credit limit defaults to 5000 and cannot be negative.
- **Source:** `V1__init.sql:5`.
- **Notes:** The default applies only if the column is omitted from the INSERT. See RULE-017.

### RULE-012 — New order starts as NEW
- **Statement:** Every created order has status `NEW` and total equal to the client-supplied amount.
- **Source:** `OrderService.java:47-49`, `Order.java:33-36` (`@PrePersist` fallback).
- **Notes:** The total comes from the request, not from order lines or stock prices. **DEFECT-01:** `create` never calls `setCustomer`, but `Order.customer` is `optional = false` and `customer_id` is `NOT NULL`, so every create attempt that passes validation should fail on save. It also never builds `OrderLine`s, so `sku` and `quantity` are validated and then discarded.

### RULE-013 — Shipped orders cannot be cancelled
- **Statement:** Cancelling an order in status `SHIPPED` fails with "A shipped order cannot be cancelled". Unknown ids give `NotFoundException`.
- **Source:** `OrderService.java:55-62`.
- **Notes:** Cancelling an already `CANCELLED` order is accepted silently (idempotent). `PAID` orders can be cancelled with no refund or stock-release logic. `markCancelled` is a bulk JPQL update that bypasses the loaded entity and compares the enum field to the string literal `'CANCELLED'`, which may not be accepted by the JPA provider (**Inference**; confirm by running).

### RULE-014 — Only ORDER_MANAGER may cancel
- **Statement:** `POST /orders/{id}/cancel` requires role `ORDER_MANAGER`.
- **Source:** `OrderController.java:28-30` (`@PreAuthorize`).
- **Notes:** Create, list and get orders, all customer endpoints and `/inventory/levels` have no role restriction in this code. Whether method security is enabled and what security config exists is not visible in this extract.

### RULE-015 — Order status changes are audited by trigger
- **Statement:** After each `UPDATE` of an order row, a row `(order_id, status)` is written to `order_audit`.
- **Source:** `V1__init.sql:31-39`.
- **Notes:** **DEFECT-03:** `order_audit` is not created anywhere in the migration, so the first UPDATE on `orders` (including cancel) would fail. The trigger fires on every update, not only status changes, and not on insert, so the initial `NEW` status is never audited.

### RULE-016 — Order-created event drives stock reservation
- **Statement:** After saving an order, the service publishes an event, and the inventory listener reserves stock.
- **Source:** `OrderService.java:51`, `OrderEventsListener.kt:9-12`, `application.yml:4-6`.
- **Notes:** Three independent defects, each of which breaks the flow:
  1. `kafkaTemplate.send("${app.topics.order-created}", …)` passes the placeholder as a literal string. Placeholders are resolved in annotations, not at runtime, so the event goes to a topic literally named `${app.topics.order-created}` while the listener reads `orders.created`.
  2. The payload is `String.valueOf(status)`, so `"NEW"`. The listener treats `substringBefore(":")` as a SKU, so it would try to reserve SKU `NEW`, which fails as an unknown SKU.
  3. The listener always reserves quantity 1, ignoring the ordered quantity.
  - The event is also sent inside the DB transaction, before commit, so a rollback can leave an event for an order that does not exist. A listener failure is not compensated (the order stays `NEW`).

### RULE-017 — Customer registration accepts the raw payload
- **Statement:** `POST /customers` saves the request body as is: no validation, uniqueness pre-check or defaults.
- **Source:** `CustomerController.java:16-17`, `CustomerService.java:29-31`.
- **Notes:** **Inference** (Jackson/Hibernate behaviour, not executed): `active` is a primitive `boolean`, so an omitted field becomes `false` and is inserted explicitly, overriding the DB default `TRUE`. New customers would then be refused by RULE-006. An omitted `creditLimit` is inserted as `NULL`, violating `NOT NULL`, so the 5000 default of RULE-011 would not apply. `Customer` has no getter or setter for `email` or `id`, so email may not bind from JSON at all. Callers can also set their own `active` and `creditLimit`, since there is no authorization on this endpoint.

### RULE-018 — Order line quantity must be positive
- **Statement:** Order line quantity must be ≥ 1.
- **Source:** `V1__init.sql:21`, `OrderLine.java:19-20` (`@Min(1)`).
- **Notes:** Dormant: no order lines are ever created.

### RULE-019 — Stock available quantity never negative
- **Statement:** `available_qty >= 0` is enforced by the DB. `reserved` has no constraint.
- **Source:** `V1__init.sql:27`.

## Defect register

| ID | Defect | Rules | Severity |
|----|--------|-------|----------|
| DEFECT-01 | Order is saved without a customer and without lines, so create fails or loses data | RULE-012 | Critical |
| DEFECT-02 | Missing `amount` gives an NPE (HTTP 500) instead of a validation error | RULE-007 | Medium |
| DEFECT-03 | Audit trigger references a table that does not exist, so all order updates fail | RULE-015 | Critical |
| DEFECT-04 | Wrong topic literal, wrong payload format and quantity fixed at 1 in the stock event flow | RULE-016 | Critical |
| DEFECT-05 | Registration likely creates inactive customers, or fails on a null credit limit | RULE-017 | High |
| DEFECT-06 | No `@ControllerAdvice` in the extract, so `BusinessException` and `NotFoundException` would map to HTTP 500 rather than 4xx (**Inference**) | all | Medium |
| DEFECT-07 | Credit check and stock reservation are race-prone (no locking) | RULE-003, 005 | Medium |

## Open questions for a business SME

1. Should "outstanding" include `PAID` and `SHIPPED` orders (RULE-002)?
2. Is 10000 a business policy or a technical cap, and should it be per customer (RULE-001)?
3. Should cancelling a `PAID` order trigger a refund and a stock release (RULE-013, RULE-005)?
4. Where is the `NEW → PAID → SHIPPED` flow implemented? It is not in this code (RULE-009).
5. Is the intended order model one SKU per order (as the request suggests) or multiple lines (as the schema suggests)?
6. Who may register or deactivate customers, and what is the intended default for `active` (RULE-017)?

## Method and limits

- Static reading only. Nothing was compiled or run, and no tests exist in the extract, so **Inference** items are unverified.
- The frontends (`frontend-angular`, `frontend-react`) were searched and contain no rules.
- The earlier `BUSINESS_RULES.previous.md` in this folder was left untouched. This run reached the same core rules and added RULE-018 and RULE-019 and a defect register. The numbering of the first 17 rules may differ from the previous file, so do not cross-reference by ID.
- This document is an internal analysis aid. Review and validate it before sharing it outside the team.
