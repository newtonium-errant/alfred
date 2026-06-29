// Non-streaming M1 affordance: a calm "assistant is thinking" bubble shown while
// a turn is in flight. The animation is motion-safe gated (per the motion-
// accessibility contract); reduced-motion users see static dots + the sr-only
// announcement, never a dead silence (intentionally-left-blank: in-flight is an
// explicit, observable state, not a frozen UI).
export function TypingIndicator() {
  return (
    <div className="flex justify-start" data-testid="typing-indicator">
      <div
        className="flex items-center gap-1.5 rounded-2xl border border-honeydew-200 bg-cream px-4 py-3"
        aria-live="polite"
      >
        <span className="sr-only">The assistant is typing…</span>
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
