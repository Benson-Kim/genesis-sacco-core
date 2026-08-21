/// Text entry: a labelled field, and the six box OTP entry.
///
/// The prototype puts the label above the input rather than floating it
/// inside, and that is worth keeping on mobile for a specific reason: a
/// floating label vanishes into the value once the field is filled, so a
/// member reviewing what they typed before submitting has to remember what
/// each box was for. A label that stays put costs one line and removes that.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../tokens/geometry.dart';
import '../tokens/palette.dart';
import '../tokens/typography.dart';

/// The prototype's `.field`: a label, an input, and an optional message.
class GpField extends StatelessWidget {
  const GpField({
    required this.label,
    required this.controller,
    super.key,
    this.hint,
    this.keyboardType,
    this.textInputAction,
    this.enabled = true,
    this.autofillHints,
    this.onSubmitted,
    this.errorText,
    this.helperText,
  });

  final String label;
  final TextEditingController controller;
  final String? hint;
  final TextInputType? keyboardType;
  final TextInputAction? textInputAction;
  final bool enabled;
  final Iterable<String>? autofillHints;
  final ValueChanged<String>? onSubmitted;

  /// Shown in the error colour under the field.
  final String? errorText;

  /// Shown in the muted colour under the field, when there is no error.
  final String? helperText;

  @override
  Widget build(BuildContext context) {
    final String? error = errorText;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(
          label,
          style: GpTypography.labelSmall.copyWith(
            color: GpPalette.ink,
            fontWeight: FontWeight.w700,
          ),
        ),
        const SizedBox(height: GpSpace.sm),
        TextField(
          controller: controller,
          enabled: enabled,
          autocorrect: false,
          keyboardType: keyboardType,
          textInputAction: textInputAction,
          autofillHints: autofillHints,
          onSubmitted: onSubmitted,
          style: GpTypography.bodyLarge,
          decoration: InputDecoration(
            hintText: hint,
            hintStyle: GpTypography.bodyLarge.copyWith(
              color: GpPalette.sub.withValues(alpha: 0.6),
            ),
            filled: true,
            fillColor: enabled ? Colors.white : GpPalette.panel,
            contentPadding: const EdgeInsets.symmetric(
              horizontal: GpSpace.md,
              vertical: GpSpace.md,
            ),
            // The BORDER carries the error too, not just the sentence
            // underneath. InputDecoration's own errorBorder only applies
            // when its own errorText is set, and this widget renders that
            // message itself -- so without keying the border off the same
            // flag, a rejected field looked untouched and the complaint
            // read as being about something else on the screen.
            border: _border(error != null ? GpPalette.brick : GpPalette.line),
            enabledBorder:
                _border(error != null ? GpPalette.brick : GpPalette.line),
            disabledBorder: _border(GpPalette.line),
            focusedBorder:
                _border(error != null ? GpPalette.brick : GpPalette.navy),
          ),
        ),
        if (error != null || helperText != null) ...<Widget>[
          const SizedBox(height: GpSpace.sm),
          Text(
            error ?? helperText!,
            style: GpTypography.bodySmall.copyWith(
              color: error != null ? GpPalette.brick : GpPalette.sub,
              fontWeight: error != null ? FontWeight.w700 : FontWeight.w400,
            ),
          ),
        ],
      ],
    );
  }

  static OutlineInputBorder _border(Color color) => OutlineInputBorder(
        borderRadius: const BorderRadius.all(
          Radius.circular(GpRadius.field),
        ),
        borderSide: BorderSide(color: color, width: 1.5),
      );
}

/// Six single character boxes, as in the prototype's `.otpbox`.
///
/// Behaviour that separates a usable OTP entry from a frustrating one, all of
/// it here so no screen has to reimplement it:
///
/// * typing advances, and the last box does not steal a seventh character;
/// * backspace in an empty box moves back and clears the previous one, which
///   is what every person tries first after a mistype;
/// * pasting or autofilling a whole code into any box distributes it across
///   all six, because that is how an SMS code arrives on a phone;
/// * non digits never enter at all.
///
/// The value is reported as one string. The boxes are a presentation choice
/// and callers should not have to know there are six of them.
class GpOtpField extends StatefulWidget {
  const GpOtpField({
    required this.onChanged,
    super.key,
    this.length = 6,
    this.enabled = true,
    this.autofocus = true,
    this.onCompleted,
    this.hasError = false,
  });

  /// Fires on every edit with the full current value, which may be short.
  final ValueChanged<String> onChanged;

  /// Fires once the last box is filled. Screens use it to submit without
  /// making the member reach for a button they can no longer see under the
  /// keyboard.
  final ValueChanged<String>? onCompleted;

  final int length;
  final bool enabled;
  final bool autofocus;

  /// Draws every box in the error colour. Set after a rejected code, so the
  /// correction is obviously about the whole code rather than one digit.
  final bool hasError;

  @override
  State<GpOtpField> createState() => _GpOtpFieldState();
}

class _GpOtpFieldState extends State<GpOtpField> {
  late final List<TextEditingController> _controllers;
  late final List<FocusNode> _nodes;

  @override
  void initState() {
    super.initState();
    _controllers = List<TextEditingController>.generate(
      widget.length,
      (_) => TextEditingController(),
    );
    _nodes = List<FocusNode>.generate(widget.length, (_) => FocusNode());
  }

  @override
  void dispose() {
    for (final TextEditingController controller in _controllers) {
      controller.dispose();
    }
    for (final FocusNode node in _nodes) {
      node.dispose();
    }
    super.dispose();
  }

  String get _value =>
      _controllers.map((TextEditingController c) => c.text).join();

  /// Set a box without disturbing the caret. Assigning to `.text` alone
  /// collapses the selection to offset zero, which parks the caret BEFORE the
  /// character just typed; the next keystroke then overwrites it.
  void _set(int index, String character) {
    _controllers[index].value = TextEditingValue(
      text: character,
      selection: TextSelection.collapsed(offset: character.length),
    );
  }

  void _report() {
    final String value = _value;
    widget.onChanged(value);
    if (value.length == widget.length) {
      widget.onCompleted?.call(value);
    }
  }

  /// A pasted or autofilled code lands in one box as several characters.
  void _spread(String digits, int from) {
    for (int i = 0; i < widget.length; i++) {
      final int source = i - from;
      if (source >= 0 && source < digits.length) {
        _set(i, digits[source]);
      }
    }
    final int landed = (from + digits.length).clamp(0, widget.length - 1);
    _nodes[landed].requestFocus();
  }

  void _onChanged(String raw, int index) {
    final String digits = raw.replaceAll(RegExp(r'[^0-9]'), '');
    if (digits.length > 1) {
      _spread(digits, index);
      setState(() {});
      _report();
      return;
    }
    _set(index, digits);
    if (digits.isNotEmpty && index < widget.length - 1) {
      _nodes[index + 1].requestFocus();
    }
    // The filled state drives each box's border, so the rebuild is not
    // cosmetic bookkeeping -- without it a box stays outlined as empty after
    // it has been typed into.
    setState(() {});
    _report();
  }

  /// Backspace in an empty box steps back and clears, rather than doing
  /// nothing and leaving the member tapping at a box that will not empty.
  ///
  /// Caveat worth stating rather than discovering: this depends on the
  /// keyboard delivering a real backspace key event. Hardware keyboards and
  /// most Android soft keyboards do; some IMEs report deletions only as text
  /// edits, and on those the member falls back to tapping the box they want.
  /// The flow still works, it is just less pleasant, so this is an
  /// enhancement rather than a load bearing behaviour.
  KeyEventResult _onKey(FocusNode node, KeyEvent event, int index) {
    if (event is! KeyDownEvent ||
        event.logicalKey != LogicalKeyboardKey.backspace) {
      return KeyEventResult.ignored;
    }
    if (_controllers[index].text.isNotEmpty || index == 0) {
      return KeyEventResult.ignored;
    }
    _controllers[index - 1].clear();
    _nodes[index - 1].requestFocus();
    setState(() {});
    _report();
    return KeyEventResult.handled;
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      children: <Widget>[
        for (int i = 0; i < widget.length; i++) ...<Widget>[
          if (i > 0) const SizedBox(width: GpSpace.sm),
          Expanded(child: _box(i)),
        ],
      ],
    );
  }

  Widget _box(int index) {
    final bool filled = _controllers[index].text.isNotEmpty;
    final Color borderColor = widget.hasError
        ? GpPalette.brick
        : (filled ? GpPalette.navy : GpPalette.line);
    return Focus(
      onKeyEvent: (FocusNode node, KeyEvent event) =>
          _onKey(node, event, index),
      child: SizedBox(
        height: 56,
        child: TextField(
          controller: _controllers[index],
          focusNode: _nodes[index],
          enabled: widget.enabled,
          autofocus: widget.autofocus && index == 0,
          textAlign: TextAlign.center,
          keyboardType: TextInputType.number,
          // One visible character. The formatter is not a length cap on
          // input: a pasted code still arrives whole and is spread across
          // the boxes by _onChanged before this ever truncates it.
          inputFormatters: <TextInputFormatter>[
            FilteringTextInputFormatter.digitsOnly,
          ],
          autofillHints:
              index == 0 ? const <String>[AutofillHints.oneTimeCode] : null,
          style: GpTypography.headlineMedium.copyWith(fontSize: 22),
          onChanged: (String raw) => _onChanged(raw, index),
          decoration: InputDecoration(
            counterText: '',
            filled: true,
            fillColor: widget.enabled ? Colors.white : GpPalette.panel,
            contentPadding: EdgeInsets.zero,
            border: GpField._border(borderColor),
            enabledBorder: GpField._border(borderColor),
            disabledBorder: GpField._border(borderColor),
            focusedBorder: GpField._border(
              widget.hasError ? GpPalette.brick : GpPalette.navy,
            ),
          ),
        ),
      ),
    );
  }
}
