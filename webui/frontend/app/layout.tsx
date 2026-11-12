import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "SunAgent",
  description: "SunAgent web console",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "dark light",
};

// Applied before hydration so a stored light theme does not flash the
// default dark field first.
const themeScript = `try{var t=window.localStorage.getItem("sunagent-theme");document.documentElement.setAttribute("data-theme",t||"dark")}catch(e){document.documentElement.setAttribute("data-theme","dark")}`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <link
          rel="icon"
          href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='9' fill='%23c6ac8f'/></svg>"
        />
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
