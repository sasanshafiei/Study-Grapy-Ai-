/** The StudyGraph mark: three linked points, like nodes on a graph. */
export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2 text-lg font-bold tracking-[-0.01em] ${className}`}>
      <svg viewBox="0 0 24 24" className="size-6" aria-hidden>
        <path d="M5 17 L11 7 L19 13" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="5" cy="17" r="2.75" fill="var(--hl-web)" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="11" cy="7" r="2.75" fill="var(--hl-class)" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="19" cy="13" r="2.75" fill="var(--hl-external)" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      StudyGraph
    </span>
  );
}
