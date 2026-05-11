-- 029_order_ref_sequence.sql — switch order_ref naming from `ord_<hex>` to `order_<N>`
--
-- Wave PERGB-INFINITE (display polish). The legacy random-hex order_ref is
-- visually noisy (`ord_4bf8b18b792b`) and gives no signal about order order
-- of arrival. Replaces with a clean sequential `order_<N>` starting at 1.
--
-- The DB column stays TEXT (no schema change) — only the value-generation
-- contract moves from `"ord_" + uuid.uuid4().hex[:12]` to
-- `f"order_{nextval('order_ref_seq')}"` in allocator.py + pergb_service.py.
-- Existing rows keep their `ord_<hex>` refs (still valid lookups). No
-- backfill — old refs continue to resolve, new orders carry the new shape.

CREATE SEQUENCE IF NOT EXISTS order_ref_seq
    START WITH 1
    INCREMENT BY 1
    MINVALUE 1
    NO MAXVALUE
    NO CYCLE;

COMMENT ON SEQUENCE order_ref_seq IS
    'Monotonic counter for human-friendly order_ref (`order_<N>`). Generated '
    'pre-INSERT in allocator.reserve / pergb_service.reserve_pergb / topup. '
    'Gaps are still possible (concurrent TX rollback in the pergb path) — '
    'matching standard SEQUENCE semantics. allocator.reserve pulls the value '
    'after the stock-check passes, so insufficient_stock failures do not '
    'consume an id.';
