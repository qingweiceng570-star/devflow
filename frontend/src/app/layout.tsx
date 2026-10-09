import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "DevFlow AI · 研发协作", description: "有证据、可核查的研发协作助手" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
