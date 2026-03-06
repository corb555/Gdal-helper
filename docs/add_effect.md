# Biome Processor -  Add `tree_line` Effect 

This document describes the steps to add a new effect called **`tree_line`** to the 
biome creation system. To add a new effect you need to modify biome_config and
biome_processor.

## A) Update `biome_config`
`biome_config` is  the centralized authority for:
- effect names and defaults
- required driver rasters derived from enabled effects
- required palettes derived from enabled effects
- missing requirement messages that include _why_ something is required
- tuning blocks available under `drivers:` in YAML

This case study adds **tree_line**, an effect that **increases rock exposure** above a 
configurable elevation.

## 1) Effect Definition

### Behavior (what the effect will do  in `BiomeProcessor`)
- When `tree_line` is enabled, it produces a rock-weight contribution:
    - `W_tree = 0` at `elev <= tree_line.elev`
    - ramps to `W_tree = 1` at `elev >= tree_line.elev + tree_line.ramp`
- This contribution will be combined with existing rock exposure logic.

## 2) Update `biome_schema.py` (Schema)

### 2.1 Add the effect flag under `enabled.effects`
Find the `enabled.effects` schema and add:

- key: `tree_line`
- type: boolean
- default: `False` (recommended default for new effect)

Example:

```yaml
enabled:
  effects:
    tree_line: false
```

Cerberus schema change (conceptual):

BIOME_SCHEMA["enabled"]["schema"]["effects"]["schema"]["tree_line"] = {"type": "boolean", "default": False}

###2.2 Add a tuning block under top-level drivers:

Add a new driver tuning section:

```
drivers:
  tree_line:
    elev: 3200
    ramp: 200
 ```


####Schema requirements:

drivers.tree_line.elev: float (or integer allowed if you prefer), default e.g. 3200.0

drivers.tree_line.ramp: float, default e.g. 200.0, should be positive (min > 0 if you want strictness)

Cerberus schema change (conceptual):

Add "tree_line": {"type": "dict", "required": False, "schema": {"elev": {...}, "ramp": {...}}} under BIOME_SCHEMA["drivers"]["schema"]

Important design rule:
Do not add "enabled" inside drivers.tree_line. The enable switch lives only in enabled.effects.

##3) Update `biome_config.py`
###3.1 Add the enum member (EffectKey)

In EffectKey, add:

TREE_LINE = "tree_line"

This keeps effect naming centralized and avoids string literal drift.

###3.2 Ensure BiomeConfig.__str__ prints tree_line status (optional but recommended)

Add a new line in the EFFECTS summary section:

show enabled state: ✅/🚫

show tuning values: elev, ramp

Example output line format:

✅ TREE_LINE rock boost (elev=3200m, ramp=200m)


>Implementation notes:

>Use self.effect_on(EffectKey.TREE_LINE.value) to check enabled

>Use self.driver("tree_line") to read tuning values (Cerberus defaults ensure presence)

###3.3 Update required_drivers() to include tree_line dependencies

tree_line is elevation-driven using the DEM window (no extra raster), so it does not require new driver rasters beyond the always-required DEM.

Therefore:

Do not add a new driver file dependency.

Keep the existing base requirements: DEM + output.

However:

You may choose to require DEM explicitly “for tree_line” as an explanation reason.
This is optional because DEM is already required for base.

If you want the missing message to be explicit, you can add a reason:

Example: DEM required by tree_line as well as base.

That means: if tree_line is enabled, you can add(DriverKey.DEM.value, EffectKey.TREE_LINE.value).

This is strictly for better messaging; it doesn’t change behavior.

###3.4 Update required_palettes() to include tree_line implications

tree_line increases rock exposure, therefore it uses the rock palettes if enabled.

Add logic:

if tree_line is enabled:

require PaletteKey.ROCK

require PaletteKey.ROCK_RED

Reason set should include "tree_line" (i.e., EffectKey.TREE_LINE.value).

Design rule:

If CLIFF/STEEP/tree_line all contribute to rock exposure, they should all point to the same palette requirements.

###3.5 Update missing_requirements() to check both drivers and palettes using Requirement objects

Ensure missing_requirements() includes:

driver file keys from required_drivers()

palette keys from required_palettes()

And that messages are specific:

rock is missing, required for tree_line effect

slope is missing, required for cliff, steep effects

etc.

For tree_line, expected missing messages could include:

missing rock palette ramp or missing palettes_yml (depending on your palette resolution rules)

###4) Update YAML Examples / Documentation
####4.1 Minimal YAML for tree_line

Add this to example biome.yml:
`
enabled:
  effects:
    tree_line: true
drivers:
  tree_line:
    elev: 3200
    ramp: 200
`
####4.2 Rock palettes note

Since tree_line drives rock exposure, make sure your files/palettes support rock ramps:

either files.rock and files.rock_red, OR

files.palettes_yml present so rock palettes can be derived

## B) Update `biome_processor`
> to come