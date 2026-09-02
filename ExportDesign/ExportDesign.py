import adsk.core
import adsk.fusion
import copy
import html
import json
import os
import platform
import re
import subprocess
import traceback

_app      = None
_ui       = None
_handlers = []

# Populated by the export command just before the results dialog is shown.
_results_folder  = ""
_results_summary = ""

COMMAND_ID         = "ExportDesignFormats"
COMMAND_NAME       = "Export Design"
COMMAND_DESC       = "Export project and/or individual components in selected formats."
RESULTS_COMMAND_ID = "ExportDesignResults"
OPEN_FOLDER_INPUT  = "openExportFolder"
TOOLBAR_TAB_ID     = "ToolsTab"
TOOLBAR_PANEL_ID   = "ExportDesignPanel"
TOOLBAR_PANEL_NAME = "Export"

PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "preferences.json")

# Regex matching characters illegal in Windows/macOS/Linux filenames.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')

# Format definitions: (id, label, tooltip, extension, supports_per_component)
FORMATS = [
    ("exportF3D" , "F3D" , "Autodesk Fusion Archive"                , "f3d" , False),
    ("export3MF" , "3MF" , "3D Manufacturing Format"                , "3mf" , True ),
    ("exportIGES", "IGES", "Initial Graphics Exchange Specification", "iges", False),
    ("exportOBJ" , "OBJ" , "Wavefront OBJ"                          , "obj" , True ),
    ("exportSAT" , "SAT" , "ACIS SAT Solid"                         , "sat" , False),
    ("exportSMT" , "SMT" , "SMT Solid"                              , "smt" , False),
    ("exportSTEP", "STEP", "ISO STEP (AP214)"                       , "step", True ),
    ("exportSTL" , "STL" , "Stereolithography Mesh"                 , "stl" , True ),
    ("exportUSD" , "USD" , "Universal Scene Description"            , "usdz", True ),
]

COMPONENT_FORMATS = [(format_id, label, tooltip, ext) for format_id, label, tooltip, ext, per_comp in FORMATS if per_comp]

DEFAULT_PREFS = {
    "formats": {
        "exportF3D": True,
        "export3MF": True,
        "exportSTEP": True
    },
    "componentFormats": {
        "exportSTL": True
    },
    "exportComponents": True,
    "meshRefinement": "Medium",
    "overwrite": True,
    "lastFolder": "",
}

# Maps format ID to a callable that creates export options.
_EXPORT_FACTORIES = {
    "exportF3D" : lambda mgr, path, geom, ref: mgr.createFusionArchiveExportOptions(path),
    "exportIGES": lambda mgr, path, geom, ref: mgr.createIGESExportOptions(path),
    "exportSAT" : lambda mgr, path, geom, ref: mgr.createSATExportOptions(path),
    "exportSMT" : lambda mgr, path, geom, ref: mgr.createSMTExportOptions(path),
    "exportSTEP": lambda mgr, path, geom, ref: mgr.createSTEPExportOptions(path, geom),
    "exportUSD" : lambda mgr, path, geom, ref: mgr.createUSDExportOptions(geom, path),
}

def _create_mesh_opts(create_fn):
    def factory(mgr, path, geom, ref):
        opts = create_fn(mgr, geom, path)
        opts.meshRefinement = ref
        return opts
    return factory


_EXPORT_FACTORIES["export3MF"] = _create_mesh_opts(lambda mgr, geom, path: mgr.createC3MFExportOptions(geom, path))
_EXPORT_FACTORIES["exportOBJ"] = _create_mesh_opts(lambda mgr, geom, path: mgr.createOBJExportOptions(geom, path))
_EXPORT_FACTORIES["exportSTL"] = _create_mesh_opts(lambda mgr, geom, path: mgr.createSTLExportOptions(geom, path))

def load_prefs():
    if not os.path.exists(PREFS_FILE):
        return copy.deepcopy(DEFAULT_PREFS)
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except (IOError, OSError, json.JSONDecodeError, ValueError) as e:
        if _ui:
            _ui.messageBox(f"Preferences file is invalid, using defaults.\n{e}")
        return copy.deepcopy(DEFAULT_PREFS)

def save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2)
    except (IOError, OSError) as e:
        if _ui:
            _ui.messageBox(f"Could not save preferences:\n{e}")

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            cmd.isExecutedWhenPreEmpted = False
            inputs = cmd.commandInputs
            prefs = load_prefs()

            # Project format selection.
            table = inputs.addTableCommandInput("formatTable", "Project Formats", 4, "1:3:1:3")

            format_prefs = prefs.get("formats", {})
            for i, (format_id, label, tooltip, ext, per_comp) in enumerate(FORMATS):
                row               = i // 2
                base_col          = (i % 2) * 2
                default_on        = format_prefs.get(format_id, False)
                checkbox          = inputs.addBoolValueInput(format_id, "", True, "", default_on)
                checkbox.tooltip  = tooltip
                table.addCommandInput(checkbox, row, base_col)
                label             = inputs.addTextBoxCommandInput(format_id + "_label", "", label, 1, True)
                label.tooltip     = tooltip
                table.addCommandInput(label, row, base_col + 1)

            table.maximumVisibleRows = table.rowCount

            # Per-component export toggle.
            comp_enabled = prefs.get("exportComponents", True)
            inputs.addBoolValueInput("exportComponents", "Export each component individually", True, "", comp_enabled)

            # Component format selection.
            comp_table = inputs.addTableCommandInput("compFormatTable", "Component Formats", 4, "1:3:1:3")

            comp_format_prefs = prefs.get("componentFormats", {})
            for i, (format_id, label, tooltip, ext) in enumerate(COMPONENT_FORMATS):
                row                = i // 2
                base_col           = (i % 2) * 2
                comp_id            = "comp_" + format_id
                default_on         = comp_format_prefs.get(format_id, False)
                checkbox           = inputs.addBoolValueInput(comp_id, "", True, "", default_on)
                checkbox.tooltip   = tooltip
                checkbox.isEnabled = comp_enabled
                comp_table.addCommandInput(checkbox, row, base_col)
                label              = inputs.addTextBoxCommandInput(comp_id + "_label", "", label, 1, True)
                label.tooltip      = tooltip
                comp_table.addCommandInput(label, row, base_col + 1)

            comp_table.maximumVisibleRows = comp_table.rowCount

            # Mesh refinement (applies to STL/3MF/OBJ exports).
            refinement_dropdown = inputs.addDropDownCommandInput("meshRefinement", "Mesh Refinement", adsk.core.DropDownStyles.TextListDropDownStyle)
            saved_refinement = prefs.get("meshRefinement", "Medium")
            refinement_dropdown.listItems.add("Low", saved_refinement == "Low")
            refinement_dropdown.listItems.add("Medium", saved_refinement == "Medium")
            refinement_dropdown.listItems.add("High", saved_refinement == "High")

            # Overwrite setting.
            overwrite = prefs.get("overwrite", True)
            inputs.addBoolValueInput("overwrite", "Overwrite existing files", True, "", overwrite)

            on_execute = CommandExecuteHandler()
            cmd.execute.add(on_execute)
            _handlers.append(on_execute)

            on_input_changed = InputChangedHandler()
            cmd.inputChanged.add(on_input_changed)
            _handlers.append(on_input_changed)
        except Exception:
            _ui.messageBox(f"Dialog setup failed:\n{traceback.format_exc()}")

class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            inputs = args.inputs
            changed = args.input
            if changed.id == "exportComponents":
                enabled = changed.value
                for format_id, label, tooltip, ext in COMPONENT_FORMATS:
                    comp_id = "comp_" + format_id
                    checkbox = inputs.itemById(comp_id)
                    if checkbox:
                        checkbox.isEnabled = enabled
        except Exception:
            if _ui:
                _ui.messageBox(f"Input change error:\n{traceback.format_exc()}")

class CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs

            export_components = inputs.itemById("exportComponents").value
            refinement_name   = inputs.itemById("meshRefinement").selectedItem.name
            overwrite         = inputs.itemById("overwrite").value

            refinement_map = {
                "Low"   : adsk.fusion.MeshRefinementSettings.MeshRefinementLow,
                "Medium": adsk.fusion.MeshRefinementSettings.MeshRefinementMedium,
                "High"  : adsk.fusion.MeshRefinementSettings.MeshRefinementHigh,
            }
            refinement = refinement_map[refinement_name]

            # Collect selected project formats.
            selected_formats = []
            format_prefs = {}
            for format_id, label, tooltip, ext, per_comp in FORMATS:
                checked = inputs.itemById(format_id).value
                format_prefs[format_id] = checked
                if checked:
                    selected_formats.append((format_id, label, ext))

            # Collect selected component formats.
            selected_comp_formats = []
            comp_format_prefs = {}
            for format_id, label, tooltip, ext in COMPONENT_FORMATS:
                comp_id = "comp_" + format_id
                checked = inputs.itemById(comp_id).value
                comp_format_prefs[format_id] = checked
                if checked:
                    selected_comp_formats.append((format_id, label, ext))

            if not selected_formats and not (export_components and selected_comp_formats):
                _ui.messageBox("No formats selected.")
                return

            # Prompt for output folder, starting from the last-used folder when available.
            dlg = _ui.createFolderDialog()
            dlg.title = "Select Export Folder"
            last_folder = load_prefs().get("lastFolder", "")
            if last_folder and os.path.isdir(last_folder):
                dlg.initialDirectory = last_folder
            if dlg.showDialog() != adsk.core.DialogResults.DialogOK:
                return
            output_folder = dlg.folder

            # Save preferences for next use.
            save_prefs({
                "formats"         : format_prefs,
                "componentFormats": comp_format_prefs,
                "exportComponents": export_components,
                "meshRefinement"  : refinement_name,
                "overwrite"       : overwrite,
                "lastFolder"      : output_folder,
            })

            design = adsk.fusion.Design.cast(_app.activeProduct)
            project_name = _app.activeDocument.name
            export_mgr = design.exportManager
            root = design.rootComponent
            exported = []
            errors = []

            # Count total export operations for progress bar.
            total_ops = len(selected_formats)
            if export_components and selected_comp_formats:
                comp_count = sum(1 for occ in root.allOccurrences if occ.component.bRepBodies.count > 0)
                total_ops += comp_count * len(selected_comp_formats)

            progress = _ui.createProgressDialog()
            progress.cancelButtonText = "Cancel"
            progress.isBackgroundTranslucent = False
            progress.isCancelButtonShown = True
            progress.show("Exporting", f"Exporting 1 of {total_ops}...", 0, total_ops, 0)
            adsk.doEvents()

            current_op = 0

            # Project-level exports.
            for format_id, label, ext in selected_formats:
                if progress.wasCancelled:
                    break
                try:
                    path = os.path.join(output_folder, f"{project_name}.{ext}")
                    if not overwrite:
                        path = unique_path(path)
                    opts = create_export_options(export_mgr, format_id, path, root, refinement)
                    if opts:
                        export_mgr.execute(opts)
                        exported.append(os.path.basename(path))
                except Exception:
                    errors.append(f"{label} (project): {traceback.format_exc()}")
                current_op += 1
                progress.progressValue = current_op
                progress.message = f"Exporting {current_op} of {total_ops}..."
                adsk.doEvents()

            # Per-component exports.
            if export_components and selected_comp_formats and not progress.wasCancelled:
                for occurrence in root.allOccurrences:
                    if progress.wasCancelled:
                        break
                    if occurrence.component.bRepBodies.count == 0:
                        continue
                    comp_name = sanitize_filename(occurrence.name)
                    for format_id, label, ext in selected_comp_formats:
                        if progress.wasCancelled:
                            break
                        try:
                            filename = f"{project_name}-{comp_name}.{ext}"
                            path = os.path.join(output_folder, filename)
                            if not overwrite:
                                path = unique_path(path)
                            opts = create_export_options(export_mgr, format_id, path, occurrence, refinement)
                            if opts:
                                export_mgr.execute(opts)
                                exported.append(os.path.basename(path))
                        except Exception:
                            errors.append(f"{label} ({occurrence.name}): {traceback.format_exc()}")
                        current_op += 1
                        progress.progressValue = current_op
                        progress.message = f"Exporting {current_op} of {total_ops}..."
                        adsk.doEvents()

            progress.hide()

            summary = f"Exported {len(exported)} file(s) to:<br/>{_html_escape(output_folder)}"
            if errors:
                error_lines = "<br/>".join(_html_escape(e) for e in errors[:5])
                summary += f"<br/><br/>{len(errors)} error(s):<br/>{error_lines}"

            show_results_dialog(output_folder if exported else "", summary)

        except Exception:
            _ui.messageBox(f"Failed:\n{traceback.format_exc()}")

class ResultsCommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            cmd.isExecutedWhenPreEmpted = False

            # Hide the default OK button; Cancel alone acts as "Close".
            cmd.isOKButtonVisible = False

            inputs = cmd.commandInputs
            summary_box = inputs.addTextBoxCommandInput("resultsSummary", "", _results_summary, 6, True)
            summary_box.isFullWidth = True

            # Offer the open-folder action only when something was actually exported.
            if _results_folder:
                resource_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
                open_button = inputs.addBoolValueInput(OPEN_FOLDER_INPUT, "Open export folder", False, resource_folder, False)
                open_button.text = "Open export folder"

            on_input_changed = ResultsInputChangedHandler()
            cmd.inputChanged.add(on_input_changed)
            _handlers.append(on_input_changed)
        except Exception:
            if _ui:
                _ui.messageBox(f"Results dialog setup failed:\n{traceback.format_exc()}")

class ResultsInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            if args.input.id == OPEN_FOLDER_INPUT and _results_folder:
                open_in_file_browser(_results_folder)
        except Exception:
            if _ui:
                _ui.messageBox(f"Open folder error:\n{traceback.format_exc()}")

def show_results_dialog(folder, summary):
    """Show the export results with an optional button that opens the output folder in the OS file browser."""
    global _results_folder, _results_summary
    _results_folder  = folder
    _results_summary = summary

    results_def = _ui.commandDefinitions.itemById(RESULTS_COMMAND_ID)
    if results_def:
        results_def.execute()

def create_export_options(export_mgr, format_id, path, geometry, refinement):
    """Create the appropriate export options for a given format."""
    factory = _EXPORT_FACTORIES.get(format_id)
    if factory:
        return factory(export_mgr, path, geometry, refinement)
    return None

def sanitize_filename(name):
    return _INVALID_FILENAME_CHARS.sub("-", name)

def _html_escape(text):
    """Escape HTML metacharacters and convert newlines to <br/> for use in formattedText."""
    return html.escape(text).replace("\n", "<br/>")

def open_in_file_browser(folder):
    """Open the given folder in the OS file browser, ignoring failures so a bad path never aborts the export."""
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(folder)
        elif system == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass

def unique_path(filepath):
    """Append a numeric suffix if the file already exists."""
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(filepath):
        filepath = f"{base}_{counter}{ext}"
        counter += 1
    return filepath

def run(context):
    """Called by Fusion when the add-in starts."""
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        # Register the command definition.
        cmd_defs = _ui.commandDefinitions
        existing = cmd_defs.itemById(COMMAND_ID)
        if existing:
            existing.deleteMe()

        resource_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
        cmd_def = cmd_defs.addButtonDefinition(COMMAND_ID, COMMAND_NAME, COMMAND_DESC, resource_folder)
        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        # Register the results dialog command (shown after an export completes).
        existing_results = cmd_defs.itemById(RESULTS_COMMAND_ID)
        if existing_results:
            existing_results.deleteMe()

        results_def = cmd_defs.addButtonDefinition(RESULTS_COMMAND_ID, "Export Results", "Summary of the last export.")
        on_results_created = ResultsCommandCreatedHandler()
        results_def.commandCreated.add(on_results_created)
        _handlers.append(on_results_created)

        # Add a button to a custom panel in the UTILITIES tab.
        tools_tab = _ui.allToolbarTabs.itemById(TOOLBAR_TAB_ID)
        if tools_tab:
            panel = tools_tab.toolbarPanels.itemById(TOOLBAR_PANEL_ID)
            if not panel:
                panel = tools_tab.toolbarPanels.add(TOOLBAR_PANEL_ID, TOOLBAR_PANEL_NAME)
            existing_control = panel.controls.itemById(COMMAND_ID)
            if existing_control:
                existing_control.deleteMe()
            control = panel.controls.addCommand(cmd_def)
            control.isPromoted = True
            control.isPromotedByDefault = True

    except Exception:
        if _ui:
            _ui.messageBox(f"ExportDesign add-in failed to start:\n{traceback.format_exc()}")

def stop(context):
    """Called by Fusion when the add-in stops."""
    try:
        # Remove the toolbar button and panel.
        tools_tab = _ui.allToolbarTabs.itemById(TOOLBAR_TAB_ID)
        if tools_tab:
            panel = tools_tab.toolbarPanels.itemById(TOOLBAR_PANEL_ID)
            if panel:
                control = panel.controls.itemById(COMMAND_ID)
                if control:
                    control.deleteMe()
                panel.deleteMe()

        # Remove the command definition.
        cmd_def = _ui.commandDefinitions.itemById(COMMAND_ID)
        if cmd_def:
            cmd_def.deleteMe()

        results_def = _ui.commandDefinitions.itemById(RESULTS_COMMAND_ID)
        if results_def:
            results_def.deleteMe()

        _handlers.clear()

    except Exception:
        if _ui:
            _ui.messageBox(f"ExportDesign add-in failed to stop:\n{traceback.format_exc()}")
