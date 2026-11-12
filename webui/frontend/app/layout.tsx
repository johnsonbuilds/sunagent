import type { Metadata, Viewport } from "next";

import { I18nProvider } from "@/lib/i18n";

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

// Mark the document language before hydration; the React provider keeps
// it in sync afterwards, including for explicit language choices.
const localeScript = `try{var p=window.localStorage.getItem("sunagent-locale");var l=(p==="en"||p==="zh")?p:((navigator.languages&&navigator.languages[0])||navigator.language||"en").toLowerCase().indexOf("zh")===0?"zh":"en";document.documentElement.lang=l==="zh"?"zh-CN":"en"}catch(e){}`;

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
        <script dangerouslySetInnerHTML={{ __html: localeScript }} />
      </head>
      <body>
        <I18nProvider>{children}</I18nProvider>
      </body>
    </html>
  );
}
