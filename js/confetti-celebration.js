/**
 * Lightweight confetti burst (fixed overlay). Used on assessment result pages.
 */
(function () {
  "use strict";

  function Confettiful(el) {
    this.el = el;
    this.containerEl = null;
    this.confettiInterval = null;
    this.confettiColors = ["#EF2964", "#00C09D", "#2D87B0", "#48485E", "#EFFF1D"];
    this.confettiAnimations = ["slow", "medium", "fast"];
    this._setupElements();
    this._renderConfetti();
  }

  Confettiful.prototype._setupElements = function () {
    var containerEl = document.createElement("div");
    var elPosition = this.el.style.position;
    if (elPosition !== "relative" && elPosition !== "absolute" && elPosition !== "fixed") {
      this.el.style.position = "relative";
    }
    containerEl.className = "confetti-container";
    this.el.appendChild(containerEl);
    this.containerEl = containerEl;
  };

  Confettiful.prototype._renderConfetti = function () {
    var self = this;
    this.confettiInterval = setInterval(function () {
      if (!self.containerEl || !self.el.offsetWidth) return;
      var confettiEl = document.createElement("div");
      var confettiSize = Math.floor(Math.random() * 3) + 7 + "px";
      var confettiBackground =
        self.confettiColors[Math.floor(Math.random() * self.confettiColors.length)];
      var confettiLeft = Math.floor(Math.random() * self.el.offsetWidth) + "px";
      var confettiAnimation =
        self.confettiAnimations[Math.floor(Math.random() * self.confettiAnimations.length)];

      confettiEl.className = "confetti confetti--animation-" + confettiAnimation;
      confettiEl.style.left = confettiLeft;
      confettiEl.style.width = confettiSize;
      confettiEl.style.height = confettiSize;
      confettiEl.style.backgroundColor = confettiBackground;

      setTimeout(function () {
        if (confettiEl.parentNode) confettiEl.parentNode.removeChild(confettiEl);
      }, 3000);

      self.containerEl.appendChild(confettiEl);
    }, 25);
  };

  Confettiful.prototype.stop = function () {
    if (this.confettiInterval) {
      clearInterval(this.confettiInterval);
      this.confettiInterval = null;
    }
  };

  /**
   * @param {Object} opts
   * @param {number} [opts.duration=5000] ms to spawn confetti before teardown
   */
  function start(opts) {
    opts = opts || {};
    var duration = typeof opts.duration === "number" ? opts.duration : 5000;

    var root = document.createElement("div");
    root.className = "js-container ne-confetti-root";
    root.setAttribute("aria-hidden", "true");
    document.body.appendChild(root);

    var inst = new Confettiful(root);
    setTimeout(function () {
      inst.stop();
      if (root.parentNode) root.parentNode.removeChild(root);
    }, duration);
  }

  window.NurseEdgeConfetti = { start: start };
})();
