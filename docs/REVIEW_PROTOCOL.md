# Human review protocol

Define visible traits for one user-selected species before reviewing images.
Melops and cheek/eye examples are provisional, not automatic field choices.

1. Preserve individual → observation → image relations. Keep source values,
   model candidates and human decisions in separate fields. Do not merge ambiguous
   observations or treat filenames/untagged labels as verified identity.
2. Inspect the original-size sanitized preview. Assess visibility and optical
   limitations before deciding a visible trait. Hidden or ambiguous evidence stays
   unknown; unclear markings do not mean absent markings.
3. Keep measured length in millimetres separate from image pixels. Without a
   calibration reference, do not infer physical length. Preserve source sex and
   validation flags; unvalidated or unknown source labels remain unknown.
4. Record human review only after a person has reviewed the evidence. Retain
   disagreement, unknowns, reasons and revisions. Never promote model confidence
   or scripted test corrections into biological verification or human consensus.
5. Keep recaptures, sides and crops together in any split by individual. A small
   convenience sample is not a representative model evaluation dataset.

Photos, CSVs and review receipts belong in ignored local data directories. Export
only allowlisted fields without coordinates, trap identifiers or location values.
Image metadata cleanup does not remove sensitive information visible in pixels.
Any data transfer or live model request requires explicit scope and budget approval.

See [workflow boundaries](TRAITS_WORKFLOW.md) and [source semantics](FIELD_DICTIONARY.md).
