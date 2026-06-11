/**
 * Pause/resume and the kill switch. The kill switch is always visible and
 * requires an explicit confirm (ARCHITECTURE.md 6.3); it never hides behind a
 * menu. Built on the shared Button and ConfirmButton primitives so the confirm
 * step (and its auto-cancel) behaves identically to every other destructive
 * action. Buttons disable while a command is in flight so a nervous double
 * click cannot double-submit.
 */
import { Button, ConfirmButton, PauseIcon, PlayIcon, StopIcon } from "../ui";

export function Controls(props: {
  paused: boolean;
  disabled?: boolean;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
}) {
  const disabled = props.disabled ?? false;
  return (
    <div className="flex flex-wrap items-center gap-3">
      {props.paused ? (
        <Button
          variant="primary"
          icon={<PlayIcon className="h-4 w-4" />}
          onClick={props.onResume}
          disabled={disabled}
          title="let the bot open positions again"
        >
          resume
        </Button>
      ) : (
        <Button
          variant="secondary"
          icon={<PauseIcon className="h-4 w-4" />}
          onClick={props.onPause}
          disabled={disabled}
          title="stop opening new positions — open positions and their stops stay managed"
        >
          pause
        </Button>
      )}
      <ConfirmButton
        label={
          <>
            <StopIcon className="h-4 w-4" />
            kill switch
          </>
        }
        confirmLabel="flatten & halt"
        title="emergency stop: sell everything at market and halt — asks for confirmation first"
        disabled={disabled}
        onConfirm={props.onKill}
        // Standalone in the header, not inside a clickable row.
        stopPropagation={false}
      />
    </div>
  );
}
