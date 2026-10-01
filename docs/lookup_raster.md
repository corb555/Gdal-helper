# `lookup_raster`

`lookup_raster` creates a new raster by replacing each integer pixel value with an attribute read from an external
lookup table.

It is designed for categorical raster products whose pixel values are identifiers rather than the final data of
interest. A common example is an ArcGIS Raster Attribute Table (`.vat.dbf`) where the raster stores a `Value` ID and the
table contains attributes such as lithology, landform, soil class, or ecological category.

Conceptually:

```text
Raster pixel value
        ↓
Lookup table key
        ↓
Selected attribute
        ↓
Output raster
```

For example:

```text
World_Ecological_2015.tif
        +
World_Ecological_2015.tif.vat.dbf

Value → Lit_Val
        ↓
16-class lithology GeoTIFF
```

The output preserves the source raster's georeferencing and pixel grid.

---

## Why This Utility Exists

Some categorical raster datasets store a compact integer identifier in each pixel and place the useful attributes in a
separate table.

For example, the USGS World Ecological Land Units raster stores a unique ecological-facet `Value`. Its Raster Attribute
Table contains the component values used to create that facet:

```text
Value
├── Bio_Val
├── LF_Val
├── Lit_Val
└── GLC_Val
```

To create a lithology raster, each source pixel must therefore be translated through the table:

```text
source pixel Value
→ matching DBF row
→ Lit_Val
→ output pixel
```

`lookup_raster` performs that translation directly and efficiently.

---

## Relationship to `reclassify`

`lookup_raster` and `reclassify` share the same underlying raster-processing model but solve different problems.

### `lookup_raster`

Uses an external table to determine the output value:

```text
Raster ID
→ table lookup
→ attribute value
```

Example:

```text
Value 28973 → Lit_Val 2
Value 28974 → Lit_Val 2
Value 28983 → Lit_Val 2
```

### `reclassify`

Uses explicitly configured category rules:

```text
Raster category ID
→ configured class mapping
→ new category
```

Example:

```text
Lithology 2 → Siliciclastic
Lithology 5 → Evaporite
```

The two utilities are intended to be composed:

```text
World Ecological raster
→ lookup_raster: Value → Lit_Val
→ 16-class lithology raster
→ reclassify: Lit_Val 2 → siliciclastic mask
```

---

## Inputs

`lookup_raster` requires:

- an integer categorical raster;
- an external lookup table;
- the table field containing the raster key;
- the table field whose values should be written to the output; and
- an output GeoTIFF path.

The initial use case is an ArcGIS Raster Attribute Table stored as `.vat.dbf`.

Example table:

| Value | Bio_Val | LF_Val | Lit_Val | GLC_Val |
|------:|--------:|-------:|--------:|--------:|
| 28973 |      12 |     12 |       2 |      60 |
| 28974 |      12 |     12 |       2 |      70 |
| 28983 |      12 |     13 |       2 |      60 |

Selecting `Value` as the key field and `Lit_Val` as the output field produces a raster containing the `Lit_Val` values.

---

## Output

The output is a single-band GeoTIFF that retains the source raster's:

- coordinate reference system;
- transform;
- width and height;
- pixel alignment; and
- geographic extent.

`lookup_raster` performs no reprojection, resampling, or alignment.

The output data type should be appropriate for the selected table field rather than being restricted to `uint8`. This
allows the same lookup mechanism to support small categorical values as well as larger integer or numeric attributes.

---

## Processing Model

The utility is designed for large rasters and does not load the full raster into memory.

Processing is performed in blocks:

```text
Read source block
→ map integer keys through lookup table
→ write output block
→ repeat
```

The external table is converted once into an in-memory lookup array before raster processing begins.

For dense integer IDs this makes the per-pixel operation effectively:

```text
output = lookup[source]
```

The raster processing infrastructure is shared with `reclassify`.

---

## Validation

The utility should fail clearly when:

- the source raster is not integer-valued;
- the lookup table cannot be opened;
- the requested key or value field does not exist;
- table keys are duplicated;
- table keys cannot be represented as non-negative integer raster IDs;
- the output path is the same as the source path; or
- the selected output values cannot be represented by the requested output data type.

Source values that do not exist in the lookup table are written using the configured default/nodata value.

Source nodata remains nodata in the output.

---

## USGS World Ecological Land Units Example

The USGS World Ecological Land Units 2015 raster combines four 250 m inputs:

- bioclimate;
- landform;
- lithology; and
- land cover.

The raster itself stores a combined `Value` identifier. The accompanying `.vat.dbf` contains the original component
attributes.

To recover lithology:

```text
World_Ecological_2015.tif
        +
World_Ecological_2015.tif.vat.dbf
        ↓
key field: Value
value field: Lit_Val
        ↓
world_lithology.tif
```

The resulting raster contains the original 16 lithology classes.

For this dataset:

```text
Lit_Val = 2
```

represents:

```text
Siliciclastic Sedimentary Rock
```

The lithology raster can then be passed to `reclassify` to create a binary siliciclastic mask or other derived
categorical products.

---

## Design

The utility reuses the generic integer lookup machinery shared with `reclassify`:

```text
                     ┌──────────────────────────┐
                     │ Integer raster LUT engine│
                     │                          │
                     │ windowed Rasterio I/O    │
                     │ GeoTIFF output           │
                     │ nodata handling          │
                     │ progress reporting       │
                     └─────────────┬────────────┘
                                   │
                 ┌─────────────────┴─────────────────┐
                 │                                   │
            reclassify                         lookup_raster
                 │                                   │
          YAML class rules                      DBF lookup table
                 │                                   │
          categorical LUT                       attribute LUT
```

The utilities remain separate because their semantics are different even though the pixel-processing engine is the same.

`reclassify` answers:

> Which configured class should this raster category become?

`lookup_raster` answers:

> Which table attribute belongs to this raster ID?

---

## Intended Uses

The operation is useful for raster products that use external attribute tables, including:

- ecological classifications;
- lithology and geology products;
- soil maps;
- land-cover products;
- habitat classifications;
- categorical remote-sensing products; and
- ArcGIS Raster Attribute Tables.

It is particularly useful when the original raster stores a compound or opaque category ID and the desired attribute
exists only in the associated table.
