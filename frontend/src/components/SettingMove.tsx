/**
 * One strategy parameter's move, rendered `before → after` (or "set"/"removed"
 * at the ends when a field was added or dropped). Shared by the research
 * timeline and the campaign card so a promotion reads the same way wherever it
 * appears. Values arrive pre-stringified from the backend — display only, never
 * arithmetic.
 */
export function SettingMove(props: { before: string | null; after: string | null }) {
  if (props.before === null) {
    return (
      <>
        set <span className="text-emerald-600 dark:text-emerald-400">{props.after}</span>
      </>
    );
  }
  if (props.after === null) {
    return (
      <>
        <span className="text-zinc-400 line-through">{props.before}</span> removed
      </>
    );
  }
  return (
    <>
      <span className="text-zinc-400 line-through">{props.before}</span>
      {" → "}
      <span className="text-emerald-600 dark:text-emerald-400">{props.after}</span>
    </>
  );
}
