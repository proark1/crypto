/**
 * Semantic tone tokens for the UI primitives. The trading-specific tone
 * vocabulary (good / bad / warn / neutral) and its text/panel/chip class
 * maps already live in `lib/interpret`, derived from the backend's verdict
 * bands; re-exporting them here gives the `ui/` primitives a single import
 * surface for tone styling without duplicating the strings (one source of
 * truth — change the palette in one place).
 */
export type { Tone } from "../lib/interpret";
export { TONE_TEXT_CLASS, TONE_PANEL_CLASS, VERDICT_CHIP_CLASS } from "../lib/interpret";
