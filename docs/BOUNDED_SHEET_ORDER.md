# Fixed-template stock orders

This is an explicitly activated deterministic application workflow. It does not call a model or use a fixture quantity. Existing FIXTURE/LIVE API enum values remain compatible; they do not describe this parser as model inference.

Use `/ui/demo/new-stock-sheet`, or submit `新建现货订单（表格）` through normal `/inputs` with no selected order and an empty source list. The entry saves an idempotent receipt and redirects to the ordinary task page. It never runs a worker implicitly. Reusing the same receipt key and payload replays the receipt; a new page/key starts a new task.

1. Run the worker once: the task asks for a workbook.
2. Upload static XLSX files to that task. Select their IDs and answer exactly `按附件新建订单`. Run the worker a second time.
3. If stock is short, the rejected draft plan and purchase suggestions remain historical evidence. A supported correction such as `蓝 L 改成 20` changes only a uniquely matching physical row in the complete candidate. Run the worker a third time, inspect the complete new plan, then explicitly approve and separately commit it.

The default budget is three worker attempts, including unsupported answers. Uploads, approval, commit and exact receipt replay consume none. A fourth distinct input can invalidate the prior plan even when the worker budget is exhausted. There is no in-task recovery or budget reset. Start a new explicitly activated task and re-upload when the budget is exhausted or the workbook must change.

## Supported workbook

Exactly one populated sheet, named `订单`, with A1:E1 exactly `客户`, `款`, `颜色`, `尺码`, `数量`. Blank rows are ignored; every nonblank row must contain all five fields. No fill-down, populated extra columns, merged layout, other populated sheets or mixed templates. Customers must resolve by exact unique active display name; SKUs by the existing exact aliases, with unique active STOCK SKU per physical row. MTO and master-data creation are unsupported. Multiple selected files may contain disjoint SKU rows for the same customer; identical files remain separate evidence and duplicate SKUs are rejected.

Positive ASCII integer quantity validation applies to persisted cell text, not original Excel XML or formatting. Numeric XML `+20` that ingestion normalizes to persisted `20` is accepted. String `+20`, persisted `20.0`, fractions, exponent text, Unicode digits, booleans, units, zero and integer overflow are rejected. Existing static upload resource/formula checks remain, plus a checksum-verified stored-workbook merge check. This is not a raw-XML numeric lexer.

## Correction language and evidence

After removing outer ASCII spaces only, a correction must fully match:

`(黑|白|蓝|藏青) + (S|M|L|XL|XXL) + 改成 + [1-9][0-9]*`

Each ` + ` above means one or more ASCII spaces. Interior spaces are required. Reject negation, punctuation, tabs/newlines, lower-case sizes, units, leading zeros, multiple actions and ambiguous color/size matches across styles. The quantity is variable, bounded by database integer representability, not fixed to 20. No partial match or model fallback applies.

Omitted or empty correction source IDs inherit only the previously proven source set without rewriting the raw receipt. The same explicit set in any order is accepted. Added, dropped or replaced sources are unsupported. Upload alone changes nothing. A new task must re-upload its own evidence.

Original workbook bytes, checksum and five cell references remain immutable. The full original candidate and accepted answer are separately linked through the private versioned `sheet_order_v1` persistence contract in existing receipt/plan JSON. The answer uses `SRC-INPUT`, its task-local input sequence and quantity field, with the actual owned event ID in private provenance. The plan authorization hash binds validated provenance; approval and commit independently revalidate it. Correction is never approval.

Shortage proposals retain their original quantities and status. Their exact IDs are linked to their rejected plan, input and intent. A later input immediately makes those recommendations historical, labeled “历史缺货建议（旧意图，非当前采购指令）”. They are never procurement instructions or proof of present availability. Every new plan checks fresh inventory; approval and commit retain ordinary freshness and replay fences.
