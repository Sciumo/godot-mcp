@tool
class_name MCPTraffic
extends RefCounted
## Bridge-traffic token estimate for the status bar.
##
## The addon cannot see the AI client's real token usage — it only sees the
## command envelopes crossing the bridge. This turns those into a rough
## estimate of what they add to the model's context: text at ~4 chars/token,
## and PNG payloads (screenshots) at ~750 pixels/token instead of by their
## base64 length, which would overcount them by orders of magnitude.
## Server-only tools (health check, toolset gating) never cross the bridge and
## are not counted. Pure static helpers so the headless dock smoke can pin them.

const CHARS_PER_TOKEN := 4.0
const PIXELS_PER_IMAGE_TOKEN := 750.0


## Estimated tokens for one text payload that may embed PNG images. `payload`
## is the parsed form of `text` (Dictionary/Array) so embedded images can be
## found and priced by pixel count instead of by their base64 characters.
static func estimate_tokens(text: String, payload: Variant) -> int:
	var images: Array[Dictionary] = []
	_collect_images(payload, images)
	var chars := text.length()
	var image_tokens := 0.0
	for img in images:
		chars -= str(img.get("base64", "")).length()
		image_tokens += float(int(img.get("width", 0)) * int(img.get("height", 0))) / PIXELS_PER_IMAGE_TOKEN
	return int(ceil(maxf(chars, 0) / CHARS_PER_TOKEN + image_tokens))


## Compact token count for tight UI: 950 → "950", 12345 → "12.3k", 2.5M → "2.5M".
static func format_tokens(tokens: int) -> String:
	if tokens < 1000:
		return str(tokens)
	if tokens < 1_000_000:
		return "%.1fk" % (tokens / 1000.0)
	return "%.1fM" % (tokens / 1_000_000.0)


static func _collect_images(value: Variant, out: Array[Dictionary]) -> void:
	if value is Dictionary:
		var d: Dictionary = value
		if str(d.get("format", "")) == "png" and d.has("base64"):
			out.append(d)
			return
		for v: Variant in d.values():
			_collect_images(v, out)
	elif value is Array:
		for v: Variant in value:
			_collect_images(v, out)
