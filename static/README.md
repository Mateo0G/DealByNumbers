# Static assets

Place the TEN Capital Network logo here as **`logo.png`** (a horizontal
logo — icon + wordmark — with a transparent background works best; it is
displayed at 44px tall in the app header).

    static/logo.png

The app references it via `url_for('static', filename='logo.png')`. If the file
is missing, the header automatically falls back to an inline SVG brand lockup, so
the page never shows a broken image.
