'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { LogIn, UserPlus, Loader2, AlertCircle, KeyRound, Info } from 'lucide-react';
import {
  fetchMe,
  login,
  register,
  requestPasswordReset,
  resetPassword,
  clearSession,
  type User,
} from '@/lib/auth';

type Mode = 'login' | 'register' | 'forgot' | 'reset';

const inputClass =
  'mt-1.5 w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-white outline-none placeholder:text-slate-500 focus:border-accent/60';
const labelClass = 'block text-[11px] font-semibold uppercase tracking-wider text-slate-400';

const TITLES: Record<Mode, string> = {
  login: 'Sign in to VoxPath',
  register: 'Create your VoxPath account',
  forgot: 'Reset your password',
  reset: 'Set a new password',
};

/**
 * Shows the chat only to a signed-in user.
 *
 * The check is a real request to /auth/me rather than "is a token present":
 * a token can be expired or signed with a rotated secret, and finding that out
 * at sign-in is far better than mid-conversation.
 *
 * Forgotten passwords: the backend issues a one-time code and shows it in its own
 * window and log (there is no mail service), and the code sets a new password here.
 */
export function AuthGate({ children }: { children: (user: User, signOut: () => void) => React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [mode, setMode] = useState<Mode>('login');
  const [username, setUsername] = useState(''); // sign-in and reset: username or email
  const [email, setEmail] = useState(''); // sign-up only, optional
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState(''); // reset only
  const [code, setCode] = useState(''); // reset only
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetchMe()
      .then(setUser)
      .finally(() => setChecking(false));
  }, []);

  const signOut = useCallback(() => {
    clearSession();
    setUser(null);
    setUsername('');
    setEmail('');
    setPassword('');
  }, []);

  const switchTo = (next: Mode) => {
    setMode(next);
    setError(null);
    setPassword('');
    setConfirm('');
    if (next !== 'reset') {
      setCode('');
      setNotice(null);
    }
  };

  const askForCode = async () => {
    const message = await requestPasswordReset(username.trim());
    setNotice(message);
    setCode('');
    setMode('reset');
  };

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError(null);
    if (mode === 'reset' && password !== confirm) {
      setError("The two passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      if (mode === 'forgot') {
        await askForCode();
        return;
      }
      const signedIn =
        mode === 'login'
          ? await login(username, password)
          : mode === 'register'
            ? await register(username, password, email)
            : await resetPassword(username.trim(), code.trim(), password);
      setUser(signedIn);
      setPassword('');
      setConfirm('');
      setCode('');
      setNotice(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  const resendCode = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await askForCode();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not request a reset code.');
    } finally {
      setBusy(false);
    }
  };

  if (checking) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-background text-slate-400">
        <Loader2 size={18} className="animate-spin" aria-hidden="true" />
        <span className="ml-2 text-sm">Checking your session…</span>
      </div>
    );
  }

  if (user) return <>{children(user, signOut)}</>;

  const isRegister = mode === 'register';
  const submitLabel = {
    login: 'Sign in',
    register: 'Create account',
    forgot: 'Send reset code',
    reset: 'Set new password',
  }[mode];
  const SubmitIcon = mode === 'register' ? UserPlus : mode === 'login' ? LogIn : KeyRound;

  return (
    <div className="flex h-[100dvh] items-center justify-center bg-background px-4 font-sans text-slate-200">
      <div className="w-full max-w-sm rounded-2xl border border-white/10 bg-white/[0.03] p-7">
        <h1 className="text-lg font-bold text-white">{TITLES[mode]}</h1>
        <p className="mt-1 text-xs text-slate-400">
          {mode === 'forgot'
            ? 'Enter your username or email. A one-time code will be issued for your account.'
            : mode === 'reset'
              ? 'Enter the reset code and choose a new password.'
              : 'Your conversations and saved facts are private to your account.'}
        </p>

        {mode === 'reset' && (
          <div className="mt-4 flex gap-2 rounded-lg border border-accent/30 bg-accent/10 p-3 text-xs leading-relaxed text-slate-200" role="status">
            <Info size={14} className="mt-px flex-shrink-0 text-accent" aria-hidden="true" />
            <span>
              {notice ?? 'A reset code has been issued.'} It works once, for 15 minutes. Look for the line
              {' '}<span className="font-semibold">PASSWORD RESET CODE</span> in the backend window.
            </span>
          </div>
        )}

        <form onSubmit={onSubmit} className="mt-6 space-y-4">
          {mode !== 'reset' && (
            <div>
              <label htmlFor="username" className={labelClass}>
                {isRegister ? 'Username' : 'Username or email'}
              </label>
              <input
                id="username"
                type="text"
                required
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                {...(isRegister
                  ? {
                      minLength: 3,
                      maxLength: 30,
                      pattern: '[A-Za-z0-9][A-Za-z0-9._\\-]{2,29}',
                      title: 'Username must be 3-30 characters: letters, numbers, dot, dash or underscore, starting with a letter or number.',
                    }
                  : {})}
                className={inputClass}
                placeholder={isRegister ? 'e.g. krish' : 'krish or you@example.com'}
              />
            </div>
          )}

          {isRegister && (
            <div>
              <label htmlFor="email" className={labelClass}>
                Email <span className="normal-case tracking-normal text-slate-500">(optional)</span>
              </label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={inputClass}
                placeholder="you@example.com"
              />
            </div>
          )}

          {mode === 'reset' && (
            <div>
              <label htmlFor="reset-code" className={labelClass}>
                Reset code
              </label>
              <input
                id="reset-code"
                type="text"
                required
                autoComplete="one-time-code"
                autoCapitalize="characters"
                spellCheck={false}
                maxLength={20}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className={`${inputClass} font-mono tracking-widest`}
                placeholder="ABCD-EFGH"
              />
            </div>
          )}

          {mode !== 'forgot' && (
            <div>
              <div className="flex items-baseline justify-between">
                <label htmlFor="password" className={labelClass}>
                  {mode === 'reset' ? 'New password' : 'Password'}
                </label>
                {mode === 'login' && (
                  <button
                    type="button"
                    onClick={() => switchTo('forgot')}
                    className="text-[11px] text-slate-400 underline-offset-2 transition-colors hover:text-slate-200 hover:underline"
                  >
                    Forgot password?
                  </button>
                )}
              </div>
              <input
                id="password"
                type="password"
                required
                minLength={8}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={inputClass}
                placeholder={mode === 'login' ? '••••••••' : 'At least 8 characters'}
              />
            </div>
          )}

          {mode === 'reset' && (
            <div>
              <label htmlFor="confirm-password" className={labelClass}>
                Confirm new password
              </label>
              <input
                id="confirm-password"
                type="password"
                required
                minLength={8}
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                className={inputClass}
                placeholder="Type it again"
              />
            </div>
          )}

          {error && (
            <p role="alert" className="flex items-start gap-1.5 text-xs text-rose-400">
              <AlertCircle size={13} className="mt-px flex-shrink-0" aria-hidden="true" />
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-black transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {busy ? (
              <Loader2 size={15} className="animate-spin" aria-hidden="true" />
            ) : (
              <SubmitIcon size={15} aria-hidden="true" />
            )}
            {submitLabel}
          </button>
        </form>

        <div className="mt-5 flex flex-col items-center gap-2 text-xs text-slate-400">
          {mode === 'reset' && (
            <button
              type="button"
              onClick={() => void resendCode()}
              disabled={busy}
              className="transition-colors hover:text-slate-200 disabled:opacity-50"
            >
              No code, or it expired? Send a new one
            </button>
          )}
          {mode === 'login' || mode === 'register' ? (
            <button
              type="button"
              onClick={() => switchTo(isRegister ? 'login' : 'register')}
              className="transition-colors hover:text-slate-200"
            >
              {isRegister ? 'Already have an account? Sign in' : 'New here? Create an account'}
            </button>
          ) : (
            <button type="button" onClick={() => switchTo('login')} className="transition-colors hover:text-slate-200">
              Back to sign in
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
