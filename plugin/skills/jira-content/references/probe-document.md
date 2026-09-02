## Converter probe

Every construct here must survive a real write, so do NOT add a code span inside a bold
run (`` **`x`** ``): the converter emits `marks: [code, strong]` on one text node, the ADF
schema forbids that pairing, and Jira refuses the WHOLE document — the probe would stop
testing anything and start failing as a unit (measured; see SKILL.md).

Inline: **bold**, *em*, ***both***, `code`, ~~struck~~, [label \] with bracket](https://e.example.com/x).

\- this paragraph is not a list

1\. this paragraph is not ordered

\| this paragraph is not a table

\::: this paragraph is not a fence

\## this paragraph is not a heading

Arithmetic stays arithmetic: 2 \* 3 \* 4, and a path stays a path: C:\\temp\\file

:::info
**Titled info**

body of the info panel
:::

:::warning
warning body
:::

:::success
success body
:::

- outer bullet
  - nested bullet
- item with two blocks

  its second paragraph

5. fifth
6. sixth

- [x] done task
- [ ] open task

> first quoted paragraph
>
> second quoted paragraph
>
> - quoted list item
>
> ```bash
> echo hi
> ```

:::expand Hidden details
inside the expand

- with a list
:::

| Env | Version |
| --- | --- |
| prod | `1.2.3` |
| pipes | a \| b |

````
before
```
after
````

---

last paragraph
