import React, { useState } from "react";
import { Check, Copy, Eye, EyeOff, Key } from "lucide-react";

interface SecretRefProps {
  credentialRef: string | null | undefined;
  className?: string;
}

export const SecretRef: React.FC<SecretRefProps> = ({ credentialRef, className = "" }) => {
  const [showFull, setShowFull] = useState(false);
  const [copied, setCopied] = useState(false);

  if (!credentialRef) {
    return <span className="text-slate-400 text-sm font-mono italic">未配置凭据引用</span>;
  }

  const maskSecret = (ref: string): string => {
    if (ref.startsWith("env://")) {
      const varName = ref.slice(6);
      if (varName.length <= 4) return "env://****";
      return `env://${varName.slice(0, 3)}***${varName.slice(-2)}`;
    }
    return `${ref.slice(0, 4)}***`;
  };

  const handleCopy = () => {
    navigator.clipboard.writeText(credentialRef);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <span
      data-testid="secret-ref"
      className={`inline-flex items-center gap-1.5 px-2 py-1 bg-slate-100 rounded border border-slate-200 text-xs font-mono text-slate-700 ${className}`}
    >
      <Key className="w-3.5 h-3.5 text-slate-400" />
      <span>{showFull ? credentialRef : maskSecret(credentialRef)}</span>
      <button
        type="button"
        onClick={() => setShowFull(!showFull)}
        className="text-slate-400 hover:text-slate-600 focus:outline-none ml-1 cursor-pointer"
        title={showFull ? "隐藏引用名" : "显示引用名"}
      >
        {showFull ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
      </button>
      <button
        type="button"
        onClick={handleCopy}
        className="text-slate-400 hover:text-slate-600 focus:outline-none cursor-pointer"
        title="复制引用"
      >
        {copied ? <Check className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3" />}
      </button>
    </span>
  );
};
