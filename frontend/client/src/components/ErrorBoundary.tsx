import { AlertTriangle, RotateCcw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";

type ErrorBoundaryProps = { children: ReactNode };
type ErrorBoundaryState = { hasError: boolean };

export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Hazard Watch OS render error", error, info);
  }

  private reset = () => {
    this.setState({ hasError: false });
    window.location.reload();
  };

  render() {
    if (!this.state.hasError) return this.props.children;

    return (
      <main className="fatal-error" role="alert">
        <AlertTriangle size={32} aria-hidden="true" />
        <h1>Console display error</h1>
        <p>The Hazard Watch interface could not be rendered. Reload the console to recover.</p>
        <button type="button" onClick={this.reset}>
          <RotateCcw size={16} aria-hidden="true" /> Reload console
        </button>
      </main>
    );
  }
}
