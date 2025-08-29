import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys

from gdal_utility import get_resolution, process_gdal_color_file
from hillshade_util import get_all, get_filename, is_outdated, ERROR_GDAL_STEP_FAILED


class BuildCommand:
    def __init__(self):
        self.command_history = {}

    def build_command_list(self, config, overlay_key, project, region, preview):
        """
        Constructs commands for the specified overlay. The commands can be function calls or
        launch of GDAL utilities.  This also tracks the command parameters.  A command is run if
        it is older than dependency files or if parameters for the command have changed.

        Args:
            config (YMLEditor): Configuration information.
            overlay_key (str): ID of the overlay in config to process.
            project: The project for this overlay
            region: The region for this overlay
            preview (bool): Should a fullsize or preview image be produced?

        Returns:
            list[str]: List of commands to execute.
        """
        cmd_name = config.get(f"{overlay_key}.COMMAND")
        title = config.get(f"{overlay_key}.TITLE")
        suffix = config.get(f"{overlay_key}.OUTPUT")
        # Create full paths for VRT creation
        data_folder = config.get(f"GENERAL.DATA_FOLDER")
        if not data_folder.endswith("/"):
            data_folder += "/"

        force = False  # Force step regardless of dependency timestamps
        self.commands = []

        # General files
        output_file = get_filename(project, region, suffix, preview)
        dem_file = shlex.quote(get_filename(project, region, "dem", preview))

        # General parameters
        quiet = config.get("GENERAL.quiet", "")
        long_quiet = "--quiet" if quiet == "-q" else ""  # some utilities use alternate quiet switch

        gdaldem = config.get("GENERAL.gdaldem", "gdaldem")
        gdaldem_compress = config.get("GENERAL.gdaldem_compress", "")
        gdal_calc_compress = config.get("GENERAL.gdal_calc_compress", "")

        print(f"{overlay_key} Commands:")

        if cmd_name == "build_dem":
            cmd_flags_vrt = get_all(config, f"GDALBUILDVRT")
            cmd_flags_warp = get_all(config, f"GDALWARP")

            file_list = config.get(f"REGIONS.{region}.FILES")
            if isinstance(file_list, list):
                file_paths = [os.path.abspath(f"{data_folder}{f.strip()}") for f in file_list]
            elif isinstance(file_list, str):
                raw_items = file_list.strip("[]").replace(",", " ").split()
                file_paths = [os.path.abspath(f"{data_folder}{item.strip().strip('\"')}") for item
                              in raw_items]
            else:
                raise ValueError(f"Unexpected type for FILES: {type(file_list)}")

            quoted_file_paths = [shlex.quote(p) for p in file_paths]
            file_str = " ".join(quoted_file_paths)
            # ------------------------------------

            tmp_vrt = f"tmp_{output_file}"
            dem_file = shlex.quote(dem_file)

            extent = config.get(f"REGIONS.{region}.EXTENT")

            self.add_command(
                force, f"gdalbuildvrt {quiet} {cmd_flags_vrt} {tmp_vrt} {file_str}", file_paths,
                tmp_vrt
            )
            self.add_command(
                force, f"gdalwarp {quiet} {cmd_flags_warp} {extent} {tmp_vrt} {dem_file}",
                [tmp_vrt], dem_file
            )

            return self.commands

        elif cmd_name == "hillshade":
            cmd_flags = get_all(config, f"GDALDEM")
            region_flags = get_all(config, f"LAYERS.{region}.HILLSHADE")
            self.add_command(
                force, f"{gdaldem} hillshade {quiet} {cmd_flags} {region_flags} {gdaldem_compress} "
                       f"{dem_file} "
                       f"{output_file}", [dem_file], output_file
            )
        elif cmd_name == "process_gdal_color_file":
            input_file_f_str = config.get(f"{overlay_key}.INPUT_FILES")
            output_file_f_str = config.get(f"{overlay_key}.OUTPUT_FILE")

            # todo - the file_f_str can have format items, e.g. "{project}_color_ramp.txt"
            # todo - create updated strings with the formatting variables applied

            # Adjust HSV of gdaldem color ramp file

            # --- 1. Get the entire PARAMETER block as a string ---
            # This string should represent a valid Python dictionary, e.g.,
            # "{'saturation_adjust': 0.8}"
            parameter_str = config.get(f"{overlay_key}.PARAMETERS")

            # --- 2. If the section exists, build the command. Otherwise, do nothing. ---
        if parameter_str:
            # The command string now passes the dictionary string to be unpacked by the executor.
            # The f-string f"...**{adjustment_params_str}" will produce a valid call like:
            # process_gdal_color_file('file1.txt', 'file2.txt', **{'saturation_adjust': 0.8})
            # This will be correctly handled by your command runner's use of eval().
            command_str = (f"{cmd_name}('{input_file}', '{output_file}', "
                           f"**{parameter_str})")

            self.add_command(
                force, command_str, [input_file], output_file
            )
        elif cmd_name == "color_relief":
            overlay_name = config.get(f"{overlay_key}.OUTPUT").lower()
            output_file = f"{project}_{overlay_name}_color_ramp.txt"

            # Use gdaldem and color_ramp file to generate a color relief image
            cmd_flags = get_all(config, f"GDALDEM")
            region_flags = get_all(config, f"LAYERS.{region}.COLOR_RELIEF")
            self.add_command(
                force,
                f"{gdaldem} color-relief {quiet} {cmd_flags} {region_flags} {gdaldem_compress} "
                f"{dem_file} {output_file} {output_file}", [dem_file, output_file], output_file
            )

        elif cmd_name == "prepare_mask":
            # Define files
            mask_suffix = config.get(f"{overlay_key}.MASK_SUFFIX")
            mask_file = get_filename(project, region, mask_suffix, preview)
            raw = get_filename(project, region, f"raw_{mask_suffix}", preview)
            mask_raw = f"{data_folder}{raw}"
            crs_file = mask_file.replace(".tif", "_crs.tif")

            if not os.path.exists(mask_raw):
                raise FileNotFoundError(
                    f"{overlay_key}: required mask file not found: '{mask_raw}'"
                )

            #  Reproject and resample precipitation to match DEM
            xres, yres = get_resolution(dem_file)
            tr_flag = f"-tr {xres} {yres}"

            cmd_flags = get_all(config, f"GDALWARP")
            extent = config.get(f"REGIONS.{region}.EXTENT")
            self.add_command(
                force, f"gdalwarp {quiet} {cmd_flags} {extent} {tr_flag} {mask_raw} {crs_file}",
                [mask_raw], crs_file
            )

            # Scale mask region values and convert to Byte
            lower_bound = config.get(f"{overlay_key}.LOWER_BOUND")
            upper_bound = config.get(f"{overlay_key}.UPPER_BOUND")
            strength = config.get(f"{overlay_key}.BLEND_STRENGTH")

            self.add_command(
                force, f'gdal_calc.py -A {crs_file} --outfile={mask_file} '
                       f'--calc="numpy.clip(((A - {lower_bound}) * 255.0 / ({upper_bound} - '
                       f'{lower_bound}) * {strength}), 0, 255)" '
                       f'--type=Byte --overwrite --NoDataValue=None', [crs_file], mask_file
            )

        elif cmd_name == "masked_blend":
            overlay1 = config.get(f"{overlay_key}.INPUT_LAYERS.LAYER1")
            overlay1_file = get_filename(project, region, overlay1, preview)

            overlay2 = config.get(f"{overlay_key}.INPUT_LAYERS.LAYER2")
            overlay2_file = get_filename(project, region, overlay2, preview)

            mask_suffix = config.get(f"{overlay_key}.MASK_SUFFIX")
            mask_file = get_filename(project, region, mask_suffix, preview)

            merge_flags = get_all(config, f"{overlay_key}.MERGE_OPTIONS")

            channels = ['R', 'G', 'B']

            blended_channels = []

            for ch in channels:
                parameter_str = f"{merge_flags} {gdal_calc_compress} {long_quiet}"
                self.masked_blend_channel(
                    ch, parameter_str, overlay1_file, overlay2_file, mask_file, output_file
                )

            self.add_command(
                force, f"gdal_merge.py -separate -o {output_file} {' '.join(blended_channels)}",
                blended_channels, output_file
            )

        elif cmd_name == "blend_layers":
            overlay1 = config.get(f"{overlay_key}.INPUT_LAYERS.LAYER1")
            overlay1_file = get_filename(project, region, overlay1, preview)

            overlay2 = config.get(f"{overlay_key}.INPUT_LAYERS.LAYER2")
            overlay2_file = get_filename(project, region, overlay2, preview)

            calc_expression = config.get(f"{overlay_key}.CALC_EXPR")
            merge_flags = get_all(config, f"{overlay_key}.MERGE_OPTIONS")

            self.add_command(
                force, f"gdal_calc.py -B {overlay1_file} -A {overlay2_file} --allBands=B --calc=\""
                       f"{calc_expression}\" {merge_flags} {gdal_calc_compress} {long_quiet} "
                       f"--overwrite "
                       f"--outfile={output_file}", [overlay1_file, overlay2_file], output_file
            )
        else:
            raise ValueError(f"Warning - unknown command '{cmd_name}' in {overlay_key} ")

        return self.commands

    def masked_blend_channel(
            self, ch, parameter_str, overlay1_file, overlay2_file, mask_file, output_file
    ):
        channel_indices = {'R': 1, 'G': 2, 'B': 3}
        idx = channel_indices[ch]

        overlay1_band = overlay1_file.replace(".tif", f"_{ch}.tif")
        overlay2_band = overlay2_file.replace(".tif", f"_{ch}.tif")
        blend_band = output_file.replace(".tif", f"_{ch}.tif")

        command = f"gdal_translate -b {idx} {overlay1_file} {overlay1_band}"

        command = f"gdal_translate -b {idx} {overlay2_file} {overlay2_band}"

        command = (f'gdal_calc.py -A {overlay1_band} -B {overlay2_band} -C {mask_file} '
                   f'--calc="numpy.clip((C.astype(float) * A + (255 - C).astype(float) * B) / '
                   f'255.0, 0, 255)" '
                   f'{parameter_str} --overwrite --outfile={blend_band}')


def parameters_unchanged(self, cmd_str, target_file):
    """
    Returns True if the last run of this command used the same parameters.  If it didn't
    then we need to re-run the command even if the target file is newer than the dependency
    files.
    """
    # todo read command_history from disk
    # todo NOT IMPLEMENTED
    return True
    last_cmd_hash = self.command_history.get(self.history_key(target_file))
    new_cmd_hash = self.get_hash(cmd_str)

    return True if new_cmd_hash == last_cmd_hash else False


def set_command_history(self, cmd_str):
    # todo implement
    return

    # update cmd_hash history for this cmd_name in this overlay
    new_cmd_hash = self.get_hash(cmd_str)
    self.command_history.set(
        self.history_key(target_file), new_cmd_hash
    )  # todo save command_history to disk


def get_hash(self, txt: str) -> str:
    """
    Return a stable, collision-resistant SHA-256 hash of the given text.
    """
    return hashlib.sha256(txt.encode('utf-8')).hexdigest()


def history_key(self, target_file):
    return f"{Path(target_file).name}"


def add_command(self, force, cmd_string, input_files, target_file):
    """
    Adds the specified command tuple to the commands list.

    Args:
        force (bool): If True, the command will be executed regardless of file timestamps.
        cmd_string (str): The shell command to be executed.
        input_files (list[str]): List of input file paths.
        target_file (str): The output file path to be checked for freshness.
    """
    print(f"{cmd_string}")
    self.commands.append((force, cmd_string, input_files, target_file))


def run_necessary_command(self, cmd_entry) -> None:
    """
    Executes the given command if needed.  A command is run if:
    force is True
    One of the input_files is newer than the target_file
    The checksum of the command parameters has changed.

    Args:
        cmd_entry (tuple): Tuple of (force, command_string, input_files, target_file).
    """
    force, cmd_str, input_files, target_file = cmd_entry

    # See if cmd checksum matches the last run
    params_updated = self.parameters_unchanged(cmd_str, target_file)

    if not force and not params_updated and is_outdated(target_file, input_files):
        print(f"⚪ Skipping up-to-date: {cmd_str}")
        return

    print(f"🟢 Running: {cmd_str}")

    # see if cmd is an internal function or external process
    for prefix, func in DISPATCH_FUNCTIONS.items():
        if cmd_str.startswith(f"{prefix}("):
            try:
                # Run internal function
                args_str = cmd_str.removeprefix(f"{prefix}(").removesuffix(")").strip()
                args = [eval(arg.strip()) for arg in args_str.split(",")] if args_str else []
                func(*args)
            except MemoryError as e:
                print(f"❌ Error in {prefix}: {e}")
                sys.exit(ERROR_GDAL_STEP_FAILED)
            else:
                print("✅ Command successful.\n")
                self.set_command_history(cmd_str)
            return

    try:
        # Run external sub-process
        result = subprocess.run(cmd_str, shell=True)
        if result.returncode != 0:
            raise RuntimeError("Shell command failed")
    except Exception as e:
        print(f"❌ Error in shell command: {e}")
        sys.exit(ERROR_GDAL_STEP_FAILED)
    else:
        print("✅ Command successful.\n")
        self.set_command_history(cmd_str)


def skip(cmd):
    print(f"Skipping: {cmd} ")


# Dispatch map: command name → function
DISPATCH_FUNCTIONS = {
    "adjust_gdal_colors": process_gdal_color_file, "skip": skip
}
