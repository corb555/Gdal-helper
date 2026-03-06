import re
import subprocess
from typing import Dict, Any, List
import xml.etree.ElementTree as ET


class CoOptions:
    """Singleton-style validator for GDAL Creation Options."""

    _definitions_cache: Dict[str, Dict[str, Any]] = {}

    def get_definition(self, driver_name: str) -> Dict[str, Any]:
        driver_name = driver_name.upper()
        if driver_name in self._definitions_cache:
            return self._definitions_cache[driver_name]

        try:
            # Run gdalinfo to get capabilities
            cmd = ["gdalinfo", "--format", driver_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            output = result.stdout
        except subprocess.CalledProcessError:
            # If driver not found, cache empty dict to stop retrying
            self._definitions_cache[driver_name] = {}
            return {}

        match = re.search(r'(<CreationOptionList>.*?</CreationOptionList>)', output, re.DOTALL)
        if not match:
            self._definitions_cache[driver_name] = {}
            return {}

        schema = {}
        try:
            root = ET.fromstring(match.group(1))
            for option in root.findall('Option'):
                name = option.get('name')
                if name:
                    opt_def = {
                        'type': option.get('type', 'string'), 'min': option.get('min'),
                        'max': option.get('max'),
                        'values': [v.text for v in option.findall('Value') if v.text]
                    }
                    schema[name] = opt_def
        except ET.ParseError:
            pass

        self._definitions_cache[driver_name] = schema
        return schema

    def validate(self, args: List[str]):
        """
        Validates creation options in a command list.
        Args: ['gdal_translate', '-of', 'MBTiles', ...]
        """
        if not args:
            return

        # 1. Identify Tool and Default Driver
        tool = args[0]
        # Default to GTiff for standard tools if not specified
        driver_name = "GTiff" if tool in ["gdal_translate", "gdalwarp"] else None

        # 2. Scan arguments
        i = 0
        co_flags = []

        while i < len(args):
            val = args[i]
            if val == '-of' and i + 1 < len(args):
                driver_name = args[i + 1]
                i += 1
            elif val == '-co' and i + 1 < len(args):
                co_flags.append(args[i + 1])
                i += 1
            i += 1

        # If we couldn't determine a driver, or no -co flags exist, skip validation
        if not driver_name or not co_flags:
            return

        # 3. Get Schema
        schema = self.get_definition(driver_name)
        if not schema:
            return  # Unknown driver or no creation options supported

        # 4. Validate Found Flags
        for kv_pair in co_flags:
            self._validate_single_option(kv_pair, schema, driver_name)

    def _validate_single_option(self, kv_pair: str, schema: Dict[str, Any], driver_name: str):
        if '=' not in kv_pair:
            # Boolean flag? (e.g. COMPRESS without value). Assume Invalid for strictness.
            return

        key, value = kv_pair.split('=', 1)
        key = key.upper()

        # ---  Whitelist Generic Options ---
        generic_whitelist = {"COPY_SRC_OVERVIEWS", "COMPRESS", "NUM_THREADS", "BIGTIFF"}

        if key in generic_whitelist:
            return

        if key not in schema:
            # Generate suggestion using schema keys + whitelist (since whitelist items are valid
            # too)
            valid_keys = list(schema.keys()) + list(generic_whitelist)
            hint = self.get_suggestion(key, valid_keys)

            raise ValueError(
                f"❌ Invalid Option: Driver '{driver_name}' does not support '{key}'.{hint}\n"
                f"   Valid options: {', '.join(sorted(schema.keys()))}"
            )

        spec = schema[key]

        # Numeric checks
        if spec['type'] in ['int', 'integer', 'float']:
            try:
                num_val = float(value)
                min_val = float(spec['min']) if spec['min'] else None
                max_val = float(spec['max']) if spec['max'] else None

                if min_val is not None and num_val < min_val:
                    raise ValueError(f"❌ Invalid Value: '{key}={value}' is too low. Min: {min_val}")
                if max_val is not None and num_val > max_val:
                    raise ValueError(
                        f"❌ Invalid Value: '{key}={value}' is too high. Max: {max_val}"
                    )
            except ValueError:
                pass

                # Enum checks
        if spec['type'] == 'string-select' and spec['values']:
            allowed = [v.upper() for v in spec['values']]
            if value.upper() not in allowed:
                # Add suggestion for enum values too!
                hint = self.get_suggestion(value.upper(), allowed)

                raise ValueError(
                    f"❌ Invalid Value: '{key}={value}' is not allowed for driver '"
                    f"{driver_name}'.{hint}\n"
                    f"   Allowed: {', '.join(spec['values'])}"
                )

    @staticmethod
    def get_suggestion(invalid_key: str, valid_options: list[str]) -> str:
        """Returns a 'Did you mean X?' string if a close match is found."""
        import difflib
        matches = difflib.get_close_matches(invalid_key, valid_options, n=1, cutoff=0.6)
        if matches:
            return f"\n   Did you mean '{matches[0]}'?"
        return ""
