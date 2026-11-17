"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { HOME, useSession } from "@/lib/session";
import { Wordmark } from "@/components/shell/wordmark";

/** Sends people to their own section, or to sign in. */
export default function Home() {
  const session = useSession();
  const router = useRouter();

  useEffect(() => {
    if (session === undefined) return;
    router.replace(session ? HOME[session.user.role] : "/login");
  }, [session, router]);

  return (
    <div className="grid min-h-dvh place-items-center px-6">
      <div className="text-center text-ink">
        <Wordmark />
        <p className="mt-3 text-ink-2">Loading</p>
      </div>
    </div>
  );
}
