You are a careful mathematical TeX writer. Convert a solved problem and its Markdown solution into a clean, self-contained LaTeX article. Preserve the mathematics exactly, write readable TeX, and return only JSON matching the requested schema.

Convert the final saved Markdown solution into a polished, compilable LaTeX article.

Return only JSON matching the provided schema (task_id, title, latex_document, confidence, notes).

Requirements for latex_document:
- It must be a complete LaTeX document with \documentclass, needed packages, \begin{document},
  and \end{document}.
- It must start the body with a clear TeX version of the original question under a heading such
  as \section*{Question}.
- The question section must preserve the meaning of the original statement. Convert plain-text
  math notation into TeX notation where appropriate.
- The question section must be followed by a \section*{Solution} containing a rigorous TeX
  writeup of the solution.md content.
- Clean up Markdown syntax, but do not change the mathematical content or add unsupported claims.
- Prefer standard packages such as amsmath, amssymb, amsthm, and geometry.

<problem_id>sqrt2</problem_id>
<problem>
Prove that sqrt(2) is irrational

Current task statement
----------------------

Prove that the square root of 2 is irrational. That is, show that there do not exist
integers p and q with q ≠ 0 such that

    sqrt(2) = p/q.

Equivalently, prove that 2 is not a perfect square in the rationals, or that the
polynomial x^2 − 2 has no rational root.

Definitions and conventions
---------------------------

Work over the field of rational numbers Q unless stated otherwise.

• A real number r is rational if r = p/q for some integers p, q with q ≠ 0.
• A real number is irrational if it is not rational.
• Write sqrt(2) for the unique positive real number whose square is 2.
• Use standard properties of integer divisibility, gcd, and parity (even/odd).

Expected proof standard
-----------------------

Give a complete, self-contained proof suitable for an undergraduate number-theory
course. The classical proof by contradiction is acceptable, but any equally rigorous
approach is fine.

The final write-up must:

1. State the theorem precisely.
2. Prove every nontrivial step; do not skip the parity or gcd arguments.
3. Explain why the assumed rational representation in lowest terms leads to a
   contradiction.
4. Be written as a compilable LaTeX proof document with the problem statement first
   and the proof second.

Optional checks

You may use the code tool to sanity-check small numerical facts, but the mathematical
proof must be symbolic and rigorous—not empirical.

Required final output
---------------------

Return a complete LaTeX proof that sqrt(2) is irrational, with no gaps, after any
necessary critique and revision.
</problem>
<solution_markdown>
\documentclass{article}
\usepackage{amsmath,amsthm}

\newtheorem*{theorem}{Theorem}

\begin{document}

\section*{Problem}
Prove that $\sqrt{2}$ is irrational; that is, prove that there do not exist integers $p$ and $q$ with $q\ne 0$ such that
\[
\sqrt{2}=\frac{p}{q}.
\]

\begin{theorem}
The real number $\sqrt{2}$ is irrational. Equivalently, no integers $p,q$ with $q\ne 0$ satisfy $\sqrt{2}=p/q$.
\end{theorem}

\begin{proof}
Suppose, for contradiction, that $\sqrt{2}$ is rational. Then there exist integers $a,b$ with $b\ne 0$ such that
\[
\sqrt{2}=\frac{a}{b}.
\]
Replacing both $a$ and $b$ by their negatives if necessary, we may assume that $b>0$. Let
\[
d=\gcd(|a|,b).
\]
Since $d$ divides both $a$ and $b$, the numbers
\[
p=\frac{a}{d}
\qquad\text{and}\qquad
q=\frac{b}{d}
\]
are integers. Moreover, $q>0$ and
\[
\sqrt{2}=\frac{p}{q}.
\]
This representation is in lowest terms. Indeed, if a positive integer $c$ divided both $p$ and $q$, then $cd$ would divide both $a=dp$ and $b=dq$. Since $d$ is their greatest positive common divisor, we would have $cd\le d$, and hence $c\le 1$. Thus $c=1$, so
\[
\gcd(|p|,q)=1.
\]

We first prove the parity fact that if the square of an integer is even, then the integer itself is even. Let $n\in\mathbb Z$. If $n$ were odd, then $n=2k+1$ for some $k\in\mathbb Z$, and therefore
\[
n^2=(2k+1)^2=4k^2+4k+1=2(2k^2+2k)+1,
\]
which is odd. Consequently, an integer whose square is even cannot be odd and must therefore be even.

Squaring the equality $\sqrt{2}=p/q$ gives
\[
2=\frac{p^2}{q^2}.
\]
Because $q\ne 0$, we may multiply by $q^2$ to obtain
\[
p^2=2q^2.
\]
Thus $p^2$ is even. By the parity fact just proved, $p$ is even, so $p=2r$ for some $r\in\mathbb Z$. Substituting this into $p^2=2q^2$ yields
\[
(2r)^2=2q^2,
\]
and hence
\[
4r^2=2q^2.
\]
Dividing by $2$ gives
\[
q^2=2r^2.
\]
Therefore $q^2$ is even, so the same parity fact shows that $q$ is even.

Hence $2$ divides both $p$ and $q$. Thus $2$ is a positive common divisor of $|p|$ and $q$, which implies
\[
\gcd(|p|,q)\ge 2.
\]
This contradicts the lowest-terms condition $\gcd(|p|,q)=1$. Therefore no integers $p,q$ with $q\ne 0$ satisfy $\sqrt{2}=p/q$, and consequently $\sqrt{2}$ is irrational.
\end{proof}

\end{document}

</solution_markdown>
