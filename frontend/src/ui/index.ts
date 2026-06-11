/**
 * The shared UI primitive layer. Screens and components import their building
 * blocks from here — one card surface, one badge, one button family, one
 * confirm pattern, one alert, one tooltip — so styling and interaction stay
 * consistent across the app and tune in a single place.
 */
export { Alert, type AlertTone } from "./Alert";
export { Badge, type BadgeTone } from "./Badge";
export { Button, type ButtonSize, type ButtonVariant } from "./Button";
export { Card } from "./Card";
export { ConfirmButton } from "./ConfirmButton";
export { InfoTooltip } from "./InfoTooltip";
export { SectionHeader } from "./SectionHeader";
export { StatTile } from "./StatTile";
export { TONE_PANEL_CLASS, TONE_TEXT_CLASS, VERDICT_CHIP_CLASS, type Tone } from "./tone";
export * from "./icons";
export { GLOSSARY, type GlossaryTerm } from "./glossary";
