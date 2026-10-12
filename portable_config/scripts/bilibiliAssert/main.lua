-- Preserve the established b key and bilibiliAssert/toggle binding while using
-- the same renderer, saved styles and visibility preference as live danmaku.
-- The former Python conversion path had independent hard-coded 16px/.55 styles.
function asstoggle(event)
    if event then return end
    mp.commandv("script-message-to", "uosc_danmaku", "show_danmaku_keyboard")
end

mp.add_key_binding("b", "toggle", asstoggle)
