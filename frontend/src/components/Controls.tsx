import { useState } from "react";

/**
 * Pause/resume and the kill switch. The kill switch is always visible and
 * requires an explicit confirm (ARCHITECTURE.md 6.3); it never hides behind
 * a menu.
 */
export function Controls(props: {
  paused: boolean;
  onPause: () => void;
  onResume: () => void;
  onKill: () => void;
}) {
  const [confirmingKill, setConfirmingKill] = useState(false);

  return (
    <div className="flex items-center gap-3">
      {props.paused ? (
        <button
          onClick={props.onResume}
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500"
        >
          resume
        </button>
      ) : (
        <button
          onClick={props.onPause}
          className="rounded-lg bg-zinc-700 px-4 py-2 text-sm font-semibold text-white hover:bg-zinc-600"
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
            className="rounded-lg bg-red-600 px-4 py-2 text-sm font-bold text-white hover:bg-red-500"
          >
            confirm: flatten &amp; halt
          </button>
          <button
            onClick={() => {
              setConfirmingKill(false);
            }}
            className="rounded-lg bg-zinc-800 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-700"
          >
            cancel
          </button>
        </>
      ) : (
        <button
          onClick={() => {
            setConfirmingKill(true);
          }}
          className="rounded-lg border border-red-600 px-4 py-2 text-sm font-bold text-red-400 hover:bg-red-600/10"
        >
          kill switch
        </button>
      )}
    </div>
  );
}
