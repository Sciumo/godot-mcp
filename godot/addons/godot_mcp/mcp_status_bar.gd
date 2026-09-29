@tool
class_name MCPStatusBar
extends HBoxContainer
## godot-mcp status-bar item — the always-visible "MCP is installed and working"
## signal, sitting in the editor's bottom status bar (toolbar fallback).
##
## Shows: a connection dot (flashes on every command), the last command with its
## handler time and estimated tokens, and session totals (commands, ~tokens).
## Hover for the detail tooltip; click to open the MCP bottom-panel tab with the
## full communication log. Like MCPStatusDock it is a dumb, editor-independent
## Control fed by the plugin, so the headless dock smoke can verify it.

signal clicked

const FLASH_SECONDS := 0.35
const ACTIVITY_COLOR := Color(0.45, 0.75, 1.0)
const _LAST_MAX_CHARS := 48

var _dot: ColorRect
var _name_label: Label
var _last_label: Label
var _totals_label: Label

var _status: MCPStatusDock.ConnectionStatus = MCPStatusDock.ConnectionStatus.DISCONNECTED
var _server_version := ""
var _enabled_toolsets := PackedStringArray()
var _available_toolsets := 0
var _command_count := 0
var _request_tokens := 0
var _response_tokens := 0
var _last_entry := ""
var _last_time := ""
var _flash_left := 0.0


func _init() -> void:
	name = "MCPStatusBar"
	mouse_filter = Control.MOUSE_FILTER_STOP
	mouse_default_cursor_shape = Control.CURSOR_POINTING_HAND
	add_theme_constant_override("separation", 6)

	_dot = ColorRect.new()
	_dot.custom_minimum_size = Vector2(10, 10)
	_dot.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	_dot.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(_dot)

	_name_label = _add_label("MCP")
	_last_label = _add_label("")
	_totals_label = _add_label("")
	_totals_label.modulate = Color(1, 1, 1, 0.7)

	set_process(false)
	set_connection_status(MCPStatusDock.ConnectionStatus.DISCONNECTED)
	_refresh()


func _add_label(text: String) -> Label:
	var label := Label.new()
	label.text = text
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	add_child(label)
	return label


func _gui_input(event: InputEvent) -> void:
	var mb := event as InputEventMouseButton
	if mb != null and mb.pressed and mb.button_index == MOUSE_BUTTON_LEFT:
		clicked.emit()
		accept_event()


func _process(delta: float) -> void:
	_flash_left -= delta
	if _flash_left <= 0.0:
		end_flash()


func set_connection_status(status: MCPStatusDock.ConnectionStatus) -> void:
	_status = status
	if _flash_left <= 0.0:
		_dot.color = MCPStatusDock.connection_color(status)
	_refresh()


func set_server_version(version: String) -> void:
	_server_version = version
	_refresh()


func set_toolsets(enabled: PackedStringArray, available: int) -> void:
	_enabled_toolsets = enabled
	_available_toolsets = available
	_refresh()


## One command crossed the bridge: count it, show it, and flash the dot.
func record_command(command: String, exec_ms: float, request_tokens: int, response_tokens: int) -> void:
	_command_count += 1
	_request_tokens += request_tokens
	_response_tokens += response_tokens
	_last_entry = "%s  %.1f ms  ~%s tok" % [
		command.trim_prefix("cmd_"), exec_ms, MCPTraffic.format_tokens(request_tokens + response_tokens),
	]
	_last_time = Time.get_time_string_from_system().substr(0, 8)
	_flash_left = FLASH_SECONDS
	_dot.color = ACTIVITY_COLOR
	set_process(true)
	_refresh()


## Restore the dot to its connection color (the flash timer's end; public so
## the headless smoke can settle it without a running SceneTree).
func end_flash() -> void:
	_flash_left = 0.0
	set_process(false)
	_dot.color = MCPStatusDock.connection_color(_status)


func _refresh() -> void:
	var connected := _status == MCPStatusDock.ConnectionStatus.CONNECTED
	_name_label.text = "MCP" if connected else "MCP: %s" % MCPStatusDock.connection_text(_status)
	_last_label.text = _last_entry if _last_entry.length() <= _LAST_MAX_CHARS else _last_entry.left(_LAST_MAX_CHARS - 1) + "…"
	_last_label.visible = not _last_entry.is_empty()
	var total := _request_tokens + _response_tokens
	_totals_label.text = "%d · ~%s tok" % [_command_count, MCPTraffic.format_tokens(total)] if _command_count > 0 else ""
	_totals_label.visible = _command_count > 0
	tooltip_text = "\n".join(PackedStringArray([
		"godot-mcp: %s" % MCPStatusDock.connection_text(_status),
		"Server: %s" % (_server_version if not _server_version.is_empty() else "(unknown)"),
		_toolsets_line(),
		"Last: %s%s" % [_last_entry if not _last_entry.is_empty() else "(none)", " at %s" % _last_time if not _last_time.is_empty() else ""],
		"Commands this session: %d" % _command_count,
		"Estimated tokens: ~%s (sent ~%s, returned ~%s)" % [
			MCPTraffic.format_tokens(total), MCPTraffic.format_tokens(_request_tokens), MCPTraffic.format_tokens(_response_tokens),
		],
		"Estimate covers editor-bridge traffic only, not the AI client's full usage.",
		"Click to open the MCP log.",
	]))


func _toolsets_line() -> String:
	if _available_toolsets == 0:
		return "Toolsets: (not reported by the server yet)"
	return "Toolsets: %d of %d enabled: %s" % [
		_enabled_toolsets.size(), _available_toolsets, ", ".join(_enabled_toolsets),
	]


# --- Accessors used by the headless dock smoke. ---

func displayed_name() -> String:
	return _name_label.text


func displayed_last() -> String:
	return _last_label.text


func displayed_totals() -> String:
	return _totals_label.text


func displayed_dot_color() -> Color:
	return _dot.color


func total_tokens() -> int:
	return _request_tokens + _response_tokens
