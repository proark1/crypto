import { useState } from "react";

/**
 * Pause/resume and the kill switch. The kill switch is always visible and
 * requires an explicit confirm (ARCHITECTURE.md 6.3); it never hides behind
 * a menu. Buttons disable while a command is in flight so a nervous double
 * click cannot double-submit.
 */
export function Controls(props: {
  paused: boolean;
  disabled?: boolean;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
}) {
  const [confirmingKill, setConfirmingKill] = useState(false);
  const disabled = props.disabled ?? false;

  return (
    <div className="flex flex-wrap items-center gap-3">
      {props.paused ? (
        <button
          onClick={props.onResume}
          disabled={disabled}
          title="let the bot open positions again"
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
        >
          resume
        </button>
      ) : (
        <button
          onClick={props.onPause}
          disabled={disabled}
          title="stop opening new positions — open positions and their stops stay managed"
          className="rounded-lg bg-zinc-700 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-600 disabled:opacity-50"
        >
          pause
        </button>
      )}
      {confirmingKill ? (
        <>
          <button
            onClick={() => {
              setConfirmingKill(false);
              props.onKill();
            }}
            disabled={disabled}
            className="rounded-lg bg-red-600 px-4 py-2 text-sm font-bold text-white hover:bg-red-500 disabled:opacity-50"
          >
            confirm: flatten &amp; halt
          </button>
          <button
            onClick={() => {
              setConfirmingKill(false);
            }}
            disabled={disabled}
            className="rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-700 disabled:opacity-50"
          >
            cancel
          </button>
        </>
      ) : (
        <button
          onClick={() => {
            setConfirmingKill(true);
          }}
          disabled={disabled}
          title="emergency stop: sell everything at market and halt — asks for confirmation first"
          className="rounded-lg border border-red-600 px-4 py-2 text-sm font-bold text-red-400 hover:bg-red-600/10 disabled:opacity-50"
        >
          kill switch
        </button>
      )}
    </div>
  );
}
