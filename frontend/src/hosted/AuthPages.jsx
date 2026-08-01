import { useState } from "react";
import { Brand, RouteLink } from "./ui.jsx";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function AuthLayout({
  navigate,
  eyebrow,
  title,
  description,
  children,
  footer,
}) {
  return (
    <div className="auth-page">
      <header className="auth-header">
        <Brand navigate={navigate} />
      </header>
      <main className="auth-main">
        <section className="auth-card" aria-labelledby="auth-title">
          <div className="auth-heading">
            <p className="eyebrow">{eyebrow}</p>
            <h1 id="auth-title">{title}</h1>
            {description && <p>{description}</p>}
          </div>
          {children}
          {footer && <div className="auth-footer">{footer}</div>}
        </section>
        <p className="auth-assurance">
          <span aria-hidden="true">◈</span> Your watchlist is private to your
          account.
        </p>
      </main>
    </div>
  );
}
function FieldError({ id, children }) {
  return children ? (
    <span id={id} className="field-error" role="alert">
      {children}
    </span>
  ) : null;
}

export function SignupPage({ navigate, client, onEmailChange }) {
  const [values, setValues] = useState({
    email: "",
    password: "",
    confirm: "",
  });
  const [errors, setErrors] = useState({});
  const [status, setStatus] = useState("idle");
  const [serverError, setServerError] = useState("");

  const submit = async (event) => {
    event.preventDefault();
    const nextErrors = {};
    if (!EMAIL_PATTERN.test(values.email))
      nextErrors.email = "Enter a valid email address.";
    if (values.password.length < 8)
      nextErrors.password = "Use at least 8 characters.";
    if (values.confirm !== values.password)
      nextErrors.confirm = "Passwords do not match.";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;
    setStatus("saving");
    setServerError("");
    try {
      await client.signup({ email: values.email, password: values.password });
      onEmailChange(values.email);
      navigate("/verify-email");
    } catch (error) {
      setStatus("error");
      setServerError(error.message || "We couldn’t create your account.");
    }
  };

  return (
    <AuthLayout
      navigate={navigate}
      eyebrow="Start your watchlist"
      title="Create your account"
      description="Set up alerts in about two minutes. No résumé or profile upload required."
      footer={
        <p>
          Already have an account?{" "}
          <RouteLink to="/signin" navigate={navigate}>
            Sign in
          </RouteLink>
        </p>
      }
    >
      <form className="auth-form" onSubmit={submit} noValidate>
        {serverError && (
          <div className="error-banner" role="alert">
            {serverError}
          </div>
        )}
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="email"
            value={values.email}
            onChange={(event) =>
              setValues({ ...values, email: event.target.value })
            }
            aria-invalid={Boolean(errors.email)}
            aria-describedby={errors.email ? "signup-email-error" : undefined}
          />
          <FieldError id="signup-email-error">{errors.email}</FieldError>
        </label>
        <label className="field">
          <span>Password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={values.password}
            onChange={(event) =>
              setValues({ ...values, password: event.target.value })
            }
            aria-invalid={Boolean(errors.password)}
            aria-describedby={
              errors.password ? "signup-password-error" : "password-hint"
            }
          />
          <small id="password-hint">At least 8 characters</small>
          <FieldError id="signup-password-error">{errors.password}</FieldError>
        </label>
        <label className="field">
          <span>Confirm password</span>
          <input
            type="password"
            autoComplete="new-password"
            value={values.confirm}
            onChange={(event) =>
              setValues({ ...values, confirm: event.target.value })
            }
            aria-invalid={Boolean(errors.confirm)}
            aria-describedby={
              errors.confirm ? "signup-confirm-error" : undefined
            }
          />
          <FieldError id="signup-confirm-error">{errors.confirm}</FieldError>
        </label>
        <button className="primary full large" disabled={status === "saving"}>
          {status === "saving" ? "Creating account…" : "Create account"}
        </button>
        <p className="form-terms">
          By creating an account, you agree to receive the alerts you configure.
          You can pause or unsubscribe at any time.
        </p>
      </form>
    </AuthLayout>
  );
}

export function SigninPage({ navigate, client }) {
  const [values, setValues] = useState({ email: "", password: "" });
  const [errors, setErrors] = useState({});
  const [status, setStatus] = useState("idle");
  const [serverError, setServerError] = useState("");

  const submit = async (event) => {
    event.preventDefault();
    const nextErrors = {};
    if (!EMAIL_PATTERN.test(values.email))
      nextErrors.email = "Enter a valid email address.";
    if (!values.password) nextErrors.password = "Enter your password.";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;
    setStatus("saving");
    setServerError("");
    try {
      await client.login(values);
      navigate("/app/dashboard");
    } catch (error) {
      setStatus("error");
      setServerError(error.message || "Email or password was not recognized.");
    }
  };

  return (
    <AuthLayout
      navigate={navigate}
      eyebrow="Welcome back"
      title="Sign in to Internship Signal"
      description="Review new matches and keep your alerts tuned."
      footer={
        <p>
          New to Internship Signal?{" "}
          <RouteLink to="/signup" navigate={navigate}>
            Create an account
          </RouteLink>
        </p>
      }
    >
      <form className="auth-form" onSubmit={submit} noValidate>
        {serverError && (
          <div className="error-banner" role="alert">
            {serverError}
          </div>
        )}
        <label className="field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="email"
            value={values.email}
            onChange={(event) =>
              setValues({ ...values, email: event.target.value })
            }
            aria-invalid={Boolean(errors.email)}
            aria-describedby={errors.email ? "signin-email-error" : undefined}
          />
          <FieldError id="signin-email-error">{errors.email}</FieldError>
        </label>
        <label className="field">
          <span className="field-label-row">
            Password{" "}
            <RouteLink to="/forgot-password" navigate={navigate}>
              Forgot password?
            </RouteLink>
          </span>
          <input
            type="password"
            autoComplete="current-password"
            value={values.password}
            onChange={(event) =>
              setValues({ ...values, password: event.target.value })
            }
            aria-invalid={Boolean(errors.password)}
            aria-describedby={
              errors.password ? "signin-password-error" : undefined
            }
          />
          <FieldError id="signin-password-error">{errors.password}</FieldError>
        </label>
        <button className="primary full large" disabled={status === "saving"}>
          {status === "saving" ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </AuthLayout>
  );
}

export function ForgotPasswordPage({ navigate, client }) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("idle");

  const submit = async (event) => {
    event.preventDefault();
    if (!EMAIL_PATTERN.test(email)) {
      setError("Enter a valid email address.");
      return;
    }
    setError("");
    setStatus("saving");
    try {
      await client.forgotPassword({ email });
      setStatus("success");
    } catch (requestError) {
      setStatus("error");
      setError(requestError.message || "We couldn’t send the reset email.");
    }
  };

  return (
    <AuthLayout
      navigate={navigate}
      eyebrow="Account recovery"
      title="Reset your password"
      description="Enter your account email and we’ll send reset instructions."
      footer={
        <p>
          <RouteLink to="/signin" navigate={navigate}>
            ← Back to sign in
          </RouteLink>
        </p>
      }
    >
      {status === "success" ? (
        <div className="auth-success" role="status">
          <span aria-hidden="true">✓</span>
          <h2>Check your inbox</h2>
          <p>
            If an account exists for <strong>{email}</strong>, reset
            instructions are on the way.
          </p>
        </div>
      ) : (
        <form className="auth-form" onSubmit={submit} noValidate>
          <label className="field">
            <span>Email</span>
            <input
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? "forgot-email-error" : undefined}
            />
            <FieldError id="forgot-email-error">{error}</FieldError>
          </label>
          <button className="primary full large" disabled={status === "saving"}>
            {status === "saving" ? "Sending…" : "Send reset instructions"}
          </button>
        </form>
      )}
    </AuthLayout>
  );
}

export function VerificationPendingPage({ navigate, email }) {
  const [resent, setResent] = useState(false);
  return (
    <AuthLayout
      navigate={navigate}
      eyebrow="One quick check"
      title="Verify your email"
      description="Email verification helps keep alert delivery reliable."
    >
      <div className="verification-content">
        <span className="mail-mark" aria-hidden="true">
          ✉
        </span>
        <p>We sent a verification link to</p>
        <strong>{email || "your email address"}</strong>
        <p>
          Open the link in that email to continue. The link may take a minute to
          arrive.
        </p>
        <button
          className="primary full large"
          onClick={() => navigate("/onboarding")}
        >
          I’ve verified my email
        </button>
        <button className="ghost full" onClick={() => setResent(true)}>
          {resent ? "Verification email resent" : "Resend verification email"}
        </button>
        {resent && (
          <p className="success-text" role="status">
            A new verification email is on the way.
          </p>
        )}
      </div>
    </AuthLayout>
  );
}
