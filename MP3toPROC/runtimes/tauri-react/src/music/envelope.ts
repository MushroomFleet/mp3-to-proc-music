// Cancellable equal-power gain envelopes on an AudioParam.
//
// Why not setValueCurveAtTime? It throws NotSupportedError if a new curve
// overlaps an existing one, which happens whenever a transition is
// retargeted mid-fade. Dense linear ramps never throw, and because this class
// mirrors every scheduled point it always knows the exact value at any time,
// so a cancel can ramp from the true current value (no jumps).

export class Envelope {
  private pts: Array<[number, number]> = [];

  constructor(
    private readonly param: AudioParam,
    initial: number,
    at: number,
  ) {
    param.cancelScheduledValues(0);
    param.setValueAtTime(initial, at);
    this.pts = [[at, initial]];
  }

  valueAt(t: number): number {
    const p = this.pts;
    if (t <= p[0][0]) return p[0][1];
    for (let i = 1; i < p.length; i++) {
      if (t <= p[i][0]) {
        const [t0, v0] = p[i - 1];
        const [t1, v1] = p[i];
        return t1 === t0 ? v1 : v0 + ((v1 - v0) * (t - t0)) / (t1 - t0);
      }
    }
    return p[p.length - 1][1];
  }

  /** Drop everything scheduled at/after t, keeping the value continuous. */
  cancel(t: number): void {
    const v = this.valueAt(t);
    this.param.cancelScheduledValues(t);
    this.pts = this.pts.filter(([pt]) => pt < t);
    if (this.pts.length === 0) {
      this.param.setValueAtTime(v, t);
    } else {
      this.param.linearRampToValueAtTime(v, t); // matches our linear model exactly
    }
    this.pts.push([t, v]);
  }

  /** Equal-power fade from the current value at t to `to` over `dur` seconds. */
  fade(t: number, dur: number, to: number, steps = 24): void {
    const from = this.valueAt(t);
    this.cancel(t);
    if (dur <= 0) {
      this.param.setValueAtTime(to, t);
      this.pts.push([t, to]);
      return;
    }
    const up = to > from;
    for (let i = 1; i <= steps; i++) {
      const x = i / steps;
      const shape = up ? Math.sin((x * Math.PI) / 2) : 1 - Math.cos((x * Math.PI) / 2);
      const v = from + (to - from) * shape;
      const ti = t + dur * x;
      this.param.linearRampToValueAtTime(v, ti);
      this.pts.push([ti, v]);
    }
  }

  set(t: number, v: number): void {
    this.cancel(t);
    this.param.setValueAtTime(v, t);
    this.pts.push([t, v]);
  }
}
