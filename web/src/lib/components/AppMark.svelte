<script>
  /**
   * The app mark for the top-left of the bar: the same geometry as the home
   * screen icon, drawn as inline SVG.
   *
   * Not an <img> to static/favicon.svg, for two reasons. It is one glyph at
   * 22px on every page load, so a request to fetch a file we ship is waste; and
   * the icon's own ground is #0f172a, which on a #0f172a bar is an invisible
   * tile with a faint edge. Dropping the tile and letting the arrow carry the
   * accent is the same mark at a size where the tile has nothing to say.
   *
   * The geometry is the icon's, unrounded: the rounded corners there belong to
   * the tile, and there is no tile here. If the icon changes in
   * scripts/make_icons.py, this is the second place to change - deliberately,
   * because a header mark and a 180px home-screen tile are tuned separately.
   *
   * Decorative: the wordmark next to it already says the name, so it is hidden
   * from assistive tech rather than read out as a second "data-download".
   */
</script>

<svg
  class="app-mark"
  viewBox="0 0 512 512"
  aria-hidden="true"
  focusable="false"
>
  <rect x="234" y="107" width="44" height="196" rx="22" fill="currentColor" />
  <path d="M162 280 L350 280 L256 371 Z" fill="currentColor" />
  <rect x="127" y="405" width="258" height="38" rx="19" fill="currentColor" />
</svg>

<style>
  .app-mark {
    display: block;
    width: 22px;
    height: 22px;
    color: var(--accent);
  }
</style>
