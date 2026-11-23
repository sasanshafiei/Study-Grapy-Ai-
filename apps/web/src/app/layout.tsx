import type { Metadata } from "next";
import "@fontsource-variable/atkinson-hyperlegible-next/wght.css";
import "@fontsource-variable/literata/opsz.css";
import "@fontsource-variable/literata/opsz-italic.css";
import "./globals.css";
import { ToastProvider } from "@/components/ui/toast";

export const metadata: Metadata = {
  title: { default: "StudyGraph", template: "%s | StudyGraph" },
  description:
    "A study coach that answers from your class materials and open sources, and shows where every sentence came from.",
};

// Runs before the first paint so the saved theme (or the system preference)
// is applied without a flash. See apps/web/README.md, "Theme".
const themeScript = `(function(){try{var t=localStorage.getItem("sg-theme");if(t!=="light"&&t!=="dark"){t=window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"}document.documentElement.setAttribute("data-theme",t)}catch(e){}})()`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-dvh antialiased">
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
