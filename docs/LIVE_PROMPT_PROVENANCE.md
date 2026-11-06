# LIVE prompt source fidelity

`build_prompts` keeps file evidence as JSON records rather than bare excerpt
strings. Each record contains its original `source_id` and every available,
nondefault `SourceRef` field: sheet, cell range, line bounds, message index,
field, excerpt and inferred flag. Null/default fields are omitted. Missing text
stays missing; empty strings and all supplied text are preserved exactly.
There is no page-number field in the current source contract, so this change
does not create one or add PDF parsing. Existing page-like labels are preserved
as their original fields, not interpreted as page numbers.

Ordering is local to prompt preparation. Records are grouped by source identity
and sheet identity, then ordered by:

1. Recognized worksheet A1 positions, numerically by start row/column and end
   row/column. Optional dollar signs and lowercase column names are recognized
   without changing their stored spelling. Positions must fit Excel bounds;
   ranges must not run backwards. A sheet value must be present.
2. Positive line starts with absent or nondecreasing line ends.
3. Nonnegative message indices.
4. A stable serialization tie-break for otherwise unlocated records.

This ordering does not infer chronology, source priority, worksheet tab index,
missing cells, headers, row meaning or relationships between unlocated records.
Malformed locators remain verbatim evidence. Repeated records remain repeated.
Random span IDs do not determine prompt ordering. The database, API/UI source
order and locally generated extraction lineage are unchanged. No model-authored
provenance is accepted. All file fields remain untrusted data, never instructions.

## Size and safety

Explicit identity and coordinates cost input space. On the isolated durable
spreadsheet probes, a nine-cell supported SKU workbook's user prompt increased
from 139 to 1066 UTF-8 bytes; the original fifteen-cell five-column workbook
increased from 162 to 1707 bytes. System guidance increased by 206 bytes in both
cases. These are byte measurements, not measured provider token counts. The
existing conservative byte-based input estimate therefore grows by 1133 or
1751 respectively, with the same strict output schema. Repeating identity per
record keeps each association self-contained; omitting null/default fields
avoids unnecessary metadata. Large inputs may hit existing budgets earlier.
Budget checks, call limits and output limits are not increased or bypassed.

## Scope of verification

Tests inspect real prompt data, including numeric row/column order, shuffled
records and random persisted span IDs, multiple files/sheets, duplicate records,
text/line/message evidence, invalid locators and serialized embedded instructions.
The fixed-response LIVE client used in durable probes tests infrastructure only;
it does not establish model understanding, parsing accuracy or support for all
workbook layouts.

Original P0 N1→N2 is still unresolved: the original 客户/款/颜色/尺码/数量
layout is not accepted by deterministic SKU source comparison; shortage still
leaves a FAILED task whose answer is rejected; short answers still lack retained
original order context; and explicit quantity overrides still conflict with the
original sheet. A separately corrected 客户/SKU/数量 workbook can pass the
controlled approval/commit/replay journey, but that is not same-task recovery.
No fixture expansion, schema/migration, intake, approval, state transition,
commit or source-override behavior is changed here.
