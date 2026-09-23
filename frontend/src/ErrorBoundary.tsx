import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

/** Keeps a render failure inside the results panel: the header, navigation and
 * journey stepper stay usable instead of the whole tree unmounting to a blank page. */
export default class ErrorBoundary extends Component<
  { children: ReactNode; onReset: () => void },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Nothing is reported anywhere in this preview; surface it for local debugging.
    console.error("Results could not be rendered.", error, info.componentStack);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="panel-body" role="alert">
        <h3>This result could not be displayed</h3>
        <p>
          Something went wrong while presenting the outlook. No estimate is
          implied by this failure.
        </p>
        <div className="panel-actions">
          <span>Preview error · Nothing was sent anywhere</span>
          <button
            className="button primary"
            onClick={() => {
              this.setState({ failed: false });
              this.props.onReset();
            }}
          >
            Return to the start
          </button>
        </div>
      </div>
    );
  }
}
