// A calm "assistant is thinking" bubble shown while a turn is in flight. The
// animation is motion-safe gated (per the motion-accessibility contract);
// reduced-motion users see static dots + the sr-only announcement, never a dead
// silence (intentionally-left-blank: in-flight is an explicit, observable state,
// not a frozen UI). With streaming, an optional `label` surfaces live tool
// activity ("Searching the vault…") so a long turn shows what it's doing.
export function TypingIndicator({ label }: { label?: string | null } = {}) {
  const announce = label && label.trim() ? label : 'The assistant is typing…';
  return (
    <div className="flex justify-start" data-testid="typing-indicator">
      <div
        className="flex items-center gap-2 rounded-2xl border border-honeydew-200 bg-cream px-4 py-3"
        aria-live="polite"
      >
        <span className="sr-only">{announce}</span>
        {label && label.trim() && (
          <span data-testid="working-label" className="text-sm text-honeydew-600">
            {label}
          </span>
        )}
        <span
          aria-hidden="true"
          className="h-2 w-2 rounded-full bg-honeydew-400 motion-safe:animate-bounce [animation-delay:-0.3s]"
        />
        <span
          aria-hidden="true"
          className="h-2 w-2 rounded-full bg-honeydew-400 motion-safe:animate-bounce [animation-delay:-0.15s]"
        />
        <span
          aria-hidden="true"
          className="h-2 w-2 rounded-full bg-honeydew-400 motion-safe:animate-bounce"
        />
      </div>
    </div>
  );
}
