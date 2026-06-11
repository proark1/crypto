/**
 * A destructive action behind one inline confirmation step. The app had three
 * different confirm patterns — a blocking `window.confirm`, a hand-rolled
 * two-button state in the leaderboard, and another in the kill switch — and
 * the kill switch's armed state had no way to time out, so a forgotten
 * confirm sat live indefinitely. This is the single pattern: click arms it,
 * a second click confirms, and the armed state auto-cancels after a few
 * seconds so it can never linger.
 *
 * `stopPropagation` defaults on because these often sit inside clickable rows
 * whose own click navigates away — the confirm must not trigger that.
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { Button, type ButtonSize, type ButtonVariant } from "./Button";

export function ConfirmButton(props: {
  label: ReactNode;
  confirmLabel: ReactNode;
  onConfirm: () => void;
  disabled?: boolean;
  size?: ButtonSize;
  variant?: ButtonVariant;
  confirmVariant?: ButtonVariant;
  /** Native tooltip on the resting button. */
  title?: string;
  /** Auto-cancel the armed state after this many ms. 0 disables the timer. */
  timeoutMs?: number;
  stopPropagation?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const stop = props.stopPropagation ?? true;
  const timeoutMs = props.timeoutMs ?? 4000;

  useEffect(() => {
    if (!armed || timeoutMs === 0) {
      return;
    }
    timer.current = setTimeout(() => {
      setArmed(false);
    }, timeoutMs);
    return () => {
      clearTimeout(timer.current);
    };
  }, [armed, timeoutMs]);

  if (armed) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <Button
          variant={props.confirmVariant ?? "danger"}
          size={props.size}
          disabled={props.disabled}
          onClick={(event) => {
            if (stop) {
              event.stopPropagation();
            }
            setArmed(false);
            props.onConfirm();
          }}
        >
          {props.confirmLabel}
        </Button>
        <Button
          variant="secondary"
          size={props.size}
          disabled={props.disabled}
          onClick={(event) => {
            if (stop) {
              event.stopPropagation();
            }
            setArmed(false);
          }}
        >
          cancel
        </Button>
      </span>
    );
  }
  return (
    <Button
      variant={props.variant ?? "dangerOutline"}
      size={props.size}
      disabled={props.disabled}
      title={props.title}
      onClick={(event) => {
        if (stop) {
          event.stopPropagation();
        }
        setArmed(true);
      }}
    >
      {props.label}
    </Button>
  );
}
