# Business Rules — shop

## Executive summary

Rules mined from `legacy/shop` (symlink to `samples/shop`: Spring Boot Java + Kotlin backend, Angular and React frontends, one Flyway migration; 34 files, ~587 lines). All files were read directly. Every citation is relative to `legacy/shop/`. The frontends contain no business rules. They only call the API. The 17 rules below cover orders, customers and inventory, and 6 of them carry suspected defects. The most important finding is that **the order-to-inventory flow and the status audit trail cannot work as written** (RULE-005, RULE-015, RULE-016). Please validate the findings with a business SME before reusing any rule in a target design.

## Summary

| ID | Name | Category | Priority | Source | Confidence |
|----|------|----------|----------|--------|------------|
| RULE-001 | Maximum single-order amount | Calculation | P0 | `backend/src/main/java/com/acme/shop/order/OrderService.java:14,40-42` | High |
| RULE-002 | Outstanding balance excludes cancelled orders | Calculation | P1 | `backend/src/main/java/com/acme/shop/order/OrderRepository.java:13` | High |
| RULE-003 | Credit-limit check on new order | Calculation | P0 | `backend/src/main/java/com/acme/shop/order/OrderService.java:43-46` | High |
| RULE-004 | Monetary precision | Calculation | P1 | `db/migration/V1__init.sql:5,12` | High |
| RULE-005 | Stock reservation | Calculation | P0 | `backend/src/main/kotlin/com/acme/shop/inventory/InventoryService.kt:10-19` | Medium |
| RULE-006 | Order requires an active customer | Validation | P0 | `backend/src/main/java/com/acme/shop/order/OrderService.java:37-38`, `customer/CustomerService.java:17-23` | High |
| RULE-007 | Order request field validation | Validation | P1 | `backend/src/main/java/com/acme/shop/order/CreateOrderRequest.java:9-19` | High |
| RULE-008 | Order total must be positive | Validation | P1 | `backend/src/main/java/com/acme/shop/order/Order.java:26-28` | Medium |
| RULE-009 | Order status restricted to fixed set | Validation | P2 | `db/migration/V1__init.sql:14`, `order/OrderStatus.java:3` | High |
| RULE-010 | Customer email format and uniqueness | Validation | P1 | `backend/src/main/java/com/acme/shop/customer/Customer.java:13-14`, `db/migration/V1__init.sql:3` | Medium |
| RULE-011 | Credit limit defaults to 5000, never negative | Validation | P1 | `db/migration/V1__init.sql:5` | High |
| RULE-012 | New order starts as NEW | Lifecycle | P1 | `backend/src/main/java/com/acme/shop/order/OrderService.java:48`, `Order.java:33-36` | High |
| RULE-013 | Shipped orders cannot be cancelled | Lifecycle | P0 | `backend/src/main/java/com/acme/shop/order/OrderService.java:55-62` | High |
| RULE-014 | Only ORDER_MANAGER may cancel | Lifecycle | P0 | `backend/src/main/java/com/acme/shop/order/OrderController.java:28-30` | Medium |
| RULE-015 | Status changes audited by trigger | Lifecycle | P1 | `db/migration/V1__init.sql:31-39` | Medium |
| RULE-016 | Order-created event drives stock reservation | Lifecycle | P0 | `backend/src/main/java/com/acme/shop/order/OrderService.java:51`, `kotlin/.../OrderEventsListener.kt:9-12` | Low |
| RULE-017 | Customer registration accepts raw entity | Policy | P1 | `backend/src/main/java/com/acme/shop/customer/CustomerController.java:16-17` | Medium |

---

### RULE-001: Maximum single-order amount
**Category:** Calculation | **Priority:** P0
**Source:** `OrderService.java:14,40-42`
**Plain English:** An order above 10,000 is refused. Exactly 10,000 is allowed.
**Specification:**
  Given an order request with amount 10,000.01
  When it is submitted via `POST /orders`
  Then it is rejected with "Order amount exceeds the maximum of 10000" and nothing is persisted
**Parameters:** `MAX_ORDER_AMOUNT = 10000`, hardcoded, no currency.
**Edge cases:** `compareTo` is used, so scale differences are harmless. A null `amount` is not guarded and causes an NPE at line 40 (see RULE-007).
**Confidence:** High.

### RULE-002: Outstanding balance excludes cancelled orders
**Category:** Calculation | **Priority:** P1
**Source:** `OrderRepository.java:13`
**Plain English:** A customer's outstanding amount is the sum of `total_amount` of all their orders whose status is not CANCELLED. PAID and SHIPPED orders still count.
**Specification:**
  Given a customer has orders 1,000 (NEW), 2,000 (PAID) and 500 (CANCELLED)
  When the outstanding amount is computed
  Then the result is 3,000
**Edge cases:** no orders returns 0 via `COALESCE`.
**Open question for SME:** should PAID orders release credit?
**Confidence:** High on the logic, open on intent.

### RULE-003: Credit-limit check on new order
**Category:** Calculation | **Priority:** P0
**Source:** `OrderService.java:43-46`
**Plain English:** A new order is refused if outstanding (RULE-002) plus the new amount exceeds the customer's credit limit. Reaching the limit exactly is allowed.
**Specification:**
  Given outstanding 4,500 and credit limit 5,000
  When the customer orders 600
  Then it is rejected with "Customer credit limit exceeded"
**Suspected defect:** `CustomerService.creditLimit()` (`CustomerService.java:25-27`) falls back to ZERO for an unknown customer. This is unreachable because RULE-006 already throws first.
**Confidence:** High.

### RULE-004: Monetary precision
**Category:** Calculation | **Priority:** P1
**Source:** `db/migration/V1__init.sql:5,12`
**Plain English:** Credit limits and order totals are stored as `NUMERIC(12,2)`: two decimals, up to 10 integer digits. Amounts with more decimals are rounded or truncated by the database, not validated by the application.
**Confidence:** High.

### RULE-005: Stock reservation
**Category:** Calculation | **Priority:** P0
**Source:** `InventoryService.kt:10-19`
**Plain English:** Reserving N units of a SKU moves N from `availableQty` to `reserved`. N must be positive. The SKU must exist and have at least N available.
**Specification:**
  Given SKU "ABC" has available 5 and reserved 0
  When 3 are reserved
  Then available is 2 and reserved is 3
  And reserving 3 more fails with "Insufficient stock for ABC"
**Edge cases:** unknown SKU gives `IllegalStateException`. The DB also enforces `available_qty >= 0`.
**Suspected defect:** the method is not `@Transactional`, so concurrent reservations could overwrite each other (lost update). There is no version column.
**Confidence:** Medium.

### RULE-006: Order requires an active customer
**Category:** Validation | **Priority:** P0
**Source:** `OrderService.java:37-38`, `CustomerService.java:17-23`
**Plain English:** An order may only be placed for an existing customer whose `active` flag is true. Otherwise: NotFound "Customer not found" or Business "Customer account is deactivated".
**Confidence:** High.

### RULE-007: Order request field validation
**Category:** Validation | **Priority:** P1
**Source:** `CreateOrderRequest.java:9-19`
**Plain English:** `customerId` and `quantity` are mandatory. `sku` is mandatory and 3 to 40 characters. `amount` has no constraint.
**Suspected defects:**
- `amount` is not `@NotNull`, so a missing amount triggers an NPE in RULE-001.
- `quantity` has no `@Min`, so zero or negative values are accepted.
- `sku` and `quantity` are never used by `OrderService.create`. No `OrderLine` is created, and the customer is never set on the `Order` (`OrderService.java:47-50`). Since `customer_id` is mandatory (`Order.java:18`, SQL line 10), saving would fail at runtime.
**Confidence:** High.

### RULE-008: Order total must be positive
**Category:** Validation | **Priority:** P1
**Source:** `Order.java:26-28`
**Plain English:** `totalAmount` must be greater than 0 when persisted (`@Positive`). This is the only lower bound on the amount.
**Confidence:** Medium. It is enforced by the bean validator at persist time, which the code does not show.

### RULE-009: Order status restricted to fixed set
**Category:** Validation | **Priority:** P2
**Source:** `V1__init.sql:14`, `OrderStatus.java:3`
**Plain English:** Valid statuses are NEW, PAID, SHIPPED, CANCELLED. This is enforced in the enum and by a DB CHECK constraint.
**Confidence:** High.

### RULE-010: Customer email format and uniqueness
**Category:** Validation | **Priority:** P1
**Source:** `Customer.java:13-14`, `V1__init.sql:3`
**Plain English:** Email must be well-formed when present and is unique, max 200 characters, NOT NULL in the DB.
**Suspected defect:** `@Email` accepts null, so the failure for a missing email comes from the DB as a raw error. `findByEmail` exists but is never called, so duplicates surface as DB errors.
**Confidence:** Medium.

### RULE-011: Credit limit defaults to 5000, never negative
**Category:** Validation | **Priority:** P1
**Source:** `V1__init.sql:5`
**Plain English:** If no limit is provided, a customer gets 5000. A negative limit is rejected by the DB.
**Edge case:** the JPA entity maps `creditLimit` as a nullable `BigDecimal` with no default. If the API sends null explicitly, the DB default is bypassed and the insert fails on NOT NULL.
**Confidence:** High.

### RULE-012: New order starts as NEW
**Category:** Lifecycle | **Priority:** P1
**Source:** `OrderService.java:48`, `Order.java:33-36`
**Plain English:** Every order is created in NEW. `@PrePersist` also defaults a null status to NEW.
**Confidence:** High.

### RULE-013: Shipped orders cannot be cancelled
**Category:** Lifecycle | **Priority:** P0
**Source:** `OrderService.java:55-62`
**Plain English:** Cancellation is refused for SHIPPED orders ("A shipped order cannot be cancelled"). NEW, PAID and already CANCELLED orders can be cancelled. Re-cancelling is a silent no-op.
**Lifecycle (as coded):** NEW, PAID, SHIPPED, CANCELLED. **Only the cancel transition exists in code.** Nothing moves an order from NEW to PAID to SHIPPED, so those transitions happen elsewhere or are missing.
**Confidence:** High.

### RULE-014: Only ORDER_MANAGER may cancel
**Category:** Lifecycle | **Priority:** P0
**Source:** `OrderController.java:28-30`
**Plain English:** `POST /orders/{id}/cancel` requires the role ORDER_MANAGER.
**Caveats:** no security configuration is in the repo, so enforcement depends on method security being enabled elsewhere. The frontend shows the Cancel button to everyone (`order-detail.component.ts:6`). `list`, `get`, `create` and all customer and inventory endpoints have no role restriction.
**Confidence:** Medium.

### RULE-015: Status changes audited by trigger
**Category:** Lifecycle | **Priority:** P1
**Source:** `V1__init.sql:31-39`
**Plain English:** Every UPDATE on `orders` should write `(order_id, status)` to `order_audit`.
**Suspected defect:** `order_audit` is not created in this migration. If it does not exist elsewhere, every UPDATE on `orders`, including cancellation, would fail. The trigger fires on any update, not only status changes.
**Confidence:** Medium.

### RULE-016: Order-created event drives stock reservation
**Category:** Lifecycle | **Priority:** P0
**Source:** `OrderService.java:51`, `OrderEventsListener.kt:9-12`, `application.yml:6`
**Plain English:** After an order is saved, an event is published. The inventory module should then reserve 1 unit of a SKU.
**Suspected defects (the flow is likely broken):**
- The producer sends to the literal string `"${app.topics.order-created}"`, which is not resolved (`kafkaTemplate.send` does not evaluate placeholders). The listener subscribes to `orders.created`.
- The payload is the order status (e.g. "NEW"). The listener treats `payload.substringBefore(":")` as the SKU, so it would look up SKU "NEW" and fail with "Unknown SKU".
- The quantity is hardcoded to 1, regardless of the ordered quantity.
- The event is sent inside the DB transaction, so a rollback after send leaves an orphan event.
**Confidence:** Low. The intent is clear but the implementation does not match it.

### RULE-017: Customer registration accepts raw entity
**Category:** Policy | **Priority:** P1
**Source:** `CustomerController.java:16-17`
**Plain English:** `POST /customers` binds the JPA entity directly with no `@Valid`. Email format, credit limit and active flag are therefore taken from the caller. `@Email` is not triggered at the controller level, and a caller can set their own credit limit. A client can also supply an `id`.
**Confidence:** Medium.

---

## Questions for business SMEs

1. Should paying an order (PAID) release its credit (RULE-002)?
2. Is 10,000 a single global cap, or should it vary per customer or currency (RULE-001)?
3. Who moves orders to PAID and SHIPPED, and is there an intended state machine (RULE-013)?
4. Who may register customers and set credit limits (RULE-017)?
5. Is the order-to-inventory reservation meant to work today (RULE-016)?

## Notes and scope

- Rules are derived from code only, with no runtime or test evidence. Intent flagged as "suspected defect" must be confirmed.
- An earlier extraction exists in `BUSINESS_RULES.previous.md`. It was not modified and was not used as a source for this pass. A few findings here are new: the missing order customer, the unresolved topic placeholder and the missing `order_audit` table.
- No client names, credentials or regulated personal data were found in the source. Customer email is personal data, so any migration that moves customer records should be reviewed with the Data Privacy Officer.
- This is an AI-generated analysis. Please review and validate it before sharing outside the team.
