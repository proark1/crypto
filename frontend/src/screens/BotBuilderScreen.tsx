import { useCallback, useEffect, useState } from "react";

import { createBot, fetchBot, fetchBotOptions, updateBotRules } from "../api/client";
import type {
  BotOptionsResponse,
  CustomBotRules,
  EntryMode,
  StrategyFamilyOption,
} from "../api/types";
import { humanizeParamName } from "../lib/format";

/** Input drafts per family: strings for number/text fields (so partial
 * typing never fights the input), booleans for checkboxes. */
type ParamDrafts = Record<string, Record<string, string | boolean>>;

function draftsFromDefaults(families: StrategyFamilyOption[]): ParamDrafts {
  const drafts: ParamDrafts = {};
  for (const family of families) {
    const familyDrafts: Record<string, string | boolean> = {};
    for (const [key, value] of Object.entries(family.defaults)) {
      familyDrafts[key] = typeof value === "boolean" ? value : String(value);
    }
    drafts[family.family] = familyDrafts;
  }
  return drafts;
}

/** Convert one family's drafts back to JSON params, typed by its defaults.
 * Strategy parameters are periods/multipliers/flags, never money, so
 * numeric parsing here is safe. Returns an error message on bad input. */
function paramsFromDrafts(
  option: StrategyFamilyOption,
  drafts: Record<string, string | boolean>,
): { params: Record<string, unknown> } | { error: string } {
  const params: Record<string, unknown> = {};
  for (const [key, defaultValue] of Object.entries(option.defaults)) {
    const draft = drafts[key];
    if (draft === undefined) {
      continue;
    }
    if (typeof defaultValue === "boolean") {
      params[key] = draft === true;
    } else if (typeof defaultValue === "number") {
      const text = typeof draft === "string" ? draft.trim() : "";
      const parsed = Number(text);
      if (text === "" || Number.isNaN(parsed)) {
        return { error: `"${humanizeParamName(key)}" in ${option.label} must be a number` };
      }
      params[key] = parsed;
    } else {
      params[key] = typeof draft === "string" ? draft : String(draft);
    }
  }
  return { params };
}

function ParamField(props: {
  paramKey: string;
  defaultValue: unknown;
  draft: string | boolean;
  onChange: (value: string | boolean) => void;
}) {
  const label = humanizeParamName(props.paramKey);
  if (typeof props.defaultValue === "boolean") {
    return (
      <label className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
        <input
          type="checkbox"
          checked={props.draft === true}
          onChange={(event) => {
            props.onChange(event.target.checked);
          }}
        />
        {label}
      </label>
    );
  }
  return (
    <label className="text-xs text-zinc-600 dark:text-zinc-400">
      {label}
      <input
        type={typeof props.defaultValue === "number" ? "number" : "text"}
        step="any"
        value={typeof props.draft === "string" ? props.draft : String(props.draft)}
        onChange={(event) => {
          props.onChange(event.target.value);
        }}
        className="mt-1 block w-32 rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
      />
    </label>
  );
}

/**
 * Build (or edit) a custom bot by picking and mixing rules. Each rule card
 * is one strategy family with its defaults editable under "advanced
 * settings"; two or more rules unlock the entry-mode choice. Creation posts
 * the recipe and hands the new bot id back to the caller for navigation.
 */
export function BotBuilderScreen(props: {
  /** Custom bot id when editing its rules; null builds a new bot. */
  editBotId: string | null;
  onCancel: () => void;
  onSaved: (botId: string) => void;
}) {
  const [options, setOptions] = useState<BotOptionsResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [drafts, setDrafts] = useState<ParamDrafts>({});
  const [entryMode, setEntryMode] = useState<EntryMode>("any");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [editLabel, setEditLabel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const { editBotId } = props;

  useEffect(() => {
    // Read through a function: the cleanup mutates the flag from outside
    // the async closure, which direct reads would mis-narrow to false.
    const lifecycle = { cancelled: false };
    const isStale = () => lifecycle.cancelled;
    void (async () => {
      try {
        const nextOptions = await fetchBotOptions();
        if (isStale()) {
          return;
        }
        setOptions(nextOptions);
        const nextDrafts = draftsFromDefaults(nextOptions.families);
        if (editBotId !== null) {
          // Prefill from the bot's current rules; params it overrides
          // replace the defaults, everything else keeps the default.
          const detail = await fetchBot(editBotId);
          if (isStale()) {
            return;
          }
          setEditLabel(detail.summary.label);
          if (detail.strategy.kind === "custom") {
            const rules = detail.strategy.rules;
            setEntryMode(rules.entry_mode);
            setSelected(new Set(Object.keys(rules.families)));
            for (const [family, params] of Object.entries(rules.families)) {
              const familyDrafts = nextDrafts[family];
              if (familyDrafts === undefined) {
                continue;
              }
              for (const [key, value] of Object.entries(params)) {
                familyDrafts[key] = typeof value === "boolean" ? value : String(value);
              }
            }
          }
        }
        setDrafts(nextDrafts);
      } catch (caught) {
        if (!isStale()) {
          setError(
            caught instanceof Error ? caught.message : "failed to load the rule options",
          );
        }
      }
    })();
    return () => {
      lifecycle.cancelled = true;
    };
  }, [editBotId]);

  const toggleFamily = useCallback((family: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(family)) {
        next.delete(family);
      } else {
        next.add(family);
      }
      return next;
    });
  }, []);

  const setDraft = useCallback((family: string, key: string, value: string | boolean) => {
    setDrafts((current) => ({
      ...current,
      [family]: { ...current[family], [key]: value },
    }));
  }, []);

  const handleSubmit = useCallback(() => {
    if (options === null) {
      return;
    }
    const chosen = options.families.filter((option) => selected.has(option.family));
    if (chosen.length === 0) {
      setError("pick at least one rule");
      return;
    }
    if (editBotId === null && name.trim() === "") {
      setError("give the bot a name");
      return;
    }
    const families: Record<string, Record<string, unknown>> = {};
    for (const option of chosen) {
      const result = paramsFromDrafts(option, drafts[option.family] ?? {});
      if ("error" in result) {
        setError(result.error);
        return;
      }
      families[option.family] = result.params;
    }
    const rules: CustomBotRules = { families };
    if (chosen.length > 1) {
      rules.entry_mode = entryMode;
    }
    setError(null);
    setPending(true);
    void (async () => {
      try {
        if (editBotId === null) {
          const created = await createBot({
            name: name.trim(),
            ...(description.trim() === "" ? {} : { description: description.trim() }),
            rules,
          });
          props.onSaved(created.bot_id);
        } else {
          await updateBotRules(editBotId, rules);
          props.onSaved(editBotId);
        }
      } catch (caught) {
        // 400/409 details arrive in plain words; show them next to the form.
        setError(caught instanceof Error ? caught.message : "saving the bot failed");
      } finally {
        setPending(false);
      }
    })();
  }, [options, selected, editBotId, name, description, drafts, entryMode, props]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={props.onCancel}
          className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          ← back
        </button>
        <h2 className="text-xl font-bold text-zinc-900 dark:text-zinc-100">
          {editBotId === null ? "create a bot" : `edit rules — ${editLabel ?? editBotId}`}
        </h2>
      </div>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Pick one or more rules for your bot to trade by. It gets its own paper account and
        competes on the leaderboard against the other bots — no real money involved.
      </p>

      {options === null ? (
        <div className="text-sm text-zinc-500">{error ?? "loading…"}</div>
      ) : (
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            handleSubmit();
          }}
        >
          <div className="space-y-3">
            {options.families.map((option) => {
              const isSelected = selected.has(option.family);
              return (
                <div
                  key={option.family}
                  className={`rounded-xl border p-4 shadow-sm transition-colors ${
                    isSelected
                      ? "border-emerald-400 bg-emerald-50/50 dark:border-emerald-700 dark:bg-emerald-950/20"
                      : "border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
                  }`}
                >
                  <label className="flex cursor-pointer items-start gap-3">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => {
                        toggleFamily(option.family);
                      }}
                      className="mt-1"
                    />
                    <span>
                      <span className="block text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                        {option.label}
                      </span>
                      <span className="block text-xs text-zinc-500">{option.description}</span>
                    </span>
                  </label>
                  {isSelected && Object.keys(option.defaults).length > 0 && (
                    <details className="mt-3">
                      <summary className="cursor-pointer text-xs font-semibold text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300">
                        advanced settings
                      </summary>
                      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-2">
                        {Object.entries(option.defaults).map(([key, defaultValue]) => (
                          <ParamField
                            key={key}
                            paramKey={key}
                            defaultValue={defaultValue}
                            draft={drafts[option.family]?.[key] ?? ""}
                            onChange={(value) => {
                              setDraft(option.family, key, value);
                            }}
                          />
                        ))}
                      </div>
                    </details>
                  )}
                </div>
              );
            })}
          </div>

          {selected.size > 1 && (
            <fieldset className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
              <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                when should it buy?
              </legend>
              <div className="space-y-2">
                <label className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                  <input
                    type="radio"
                    name="entry-mode"
                    checked={entryMode === "any"}
                    onChange={() => {
                      setEntryMode("any");
                    }}
                  />
                  Buy when ANY rule fires (trades more)
                </label>
                <label className="flex items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300">
                  <input
                    type="radio"
                    name="entry-mode"
                    checked={entryMode === "all"}
                    onChange={() => {
                      setEntryMode("all");
                    }}
                  />
                  Buy only when ALL rules agree (trades less, higher conviction)
                </label>
              </div>
            </fieldset>
          )}

          {editBotId === null && (
            <div className="flex flex-wrap gap-4 rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
              <label className="text-xs text-zinc-600 dark:text-zinc-400">
                name
                <input
                  value={name}
                  onChange={(event) => {
                    setName(event.target.value);
                  }}
                  placeholder="e.g. My steady trender"
                  className="mt-1 block w-56 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
                />
              </label>
              <label className="text-xs text-zinc-600 dark:text-zinc-400">
                description (optional)
                <input
                  value={description}
                  onChange={(event) => {
                    setDescription(event.target.value);
                  }}
                  placeholder="we'll write one for you if you leave this empty"
                  className="mt-1 block w-72 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
                />
              </label>
            </div>
          )}

          {error !== null && (
            <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/50 dark:text-red-300">
              {error}
            </div>
          )}

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={pending}
              className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              {editBotId === null ? "create the bot" : "save the rules"}
            </button>
            <button
              type="button"
              onClick={props.onCancel}
              className="rounded-lg border border-zinc-300 px-4 py-2 text-sm text-zinc-600 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
