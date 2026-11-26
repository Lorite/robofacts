# storage.yaml

`storage.yaml` declares, per machine, an external disk for heavy recordings. This page lists the keys of the file and how the runner uses them. For the task, see the [[storage]] guide.

## Where the file is

The project has one at `experiments/common/config/storage.yaml`. `trials init` writes it with no destination. A missing file means that no destination is declared.

## A minimal example

```yaml
version: 1
external_subdir: trial_bags
hosts:
  laptop:
    external_dest: /media/me/MY_DISK/trial_bags
```

## Keys

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `hosts` | mapping | no | empty | One entry per host level, the levels of the `execution_host` factor. |
| `hosts.<level>.external_dest` | path | no | none | The external destination of that machine. `null` or an absent key means that no destination is declared. |
| `external_subdir` | string | no | `trial_bags` | The directory that the runner adds when a destination is the mount point of a disk, so the recordings do not land beside the other files of the disk. |
| `version` | integer | no | none | The templates write `version: 1`. The code does not read it. |

## Which machine is which level

The file names machines by their host level, not by their host name. The runner uses the `execution_host` level of the trial. `trials storage` maps the host name of the current machine to a level through the `aliases` of the `execution_host` factor in [[factors-yaml|factors.yaml]], so add the host name of a machine there as an alias of its level. `trials storage --host-level <level>` reports for another level.

## How the runner uses it

The setting `bags_root` of [[runner-yaml|runner.yaml]] (at the top level, or per host), or the flag `--bags-root` of `trials run`, chooses where a trial's recording goes:

| Value | Where the recording goes |
| --- | --- |
| absent, `repo`, `internal`, `default` or `none` | the experiment's own recordings folder (the default) |
| `external` | `external_dest` of the recording host, laid out as `<destination>/<experiment>/<recordings folder>` |
| a path | that directory, laid out the same way |

With an external destination, the runner leaves a symbolic link in the experiment's recordings folder. The coverage index, the slim copies and the analyses read the recording through that link while the disk is mounted. With the disk unmounted, `trials plan` and `trials run` stop with an error instead of planning without those recordings.

!!! warning
    Nothing is guessed. When `bags_root: external` names a host without `external_dest`, the runner refuses and names this file and the flag. An unmounted mount point is an ordinary directory on the internal disk, so a guessed path would fill the disk that it should relieve.

## Check what is declared

```bash
trials storage
trials storage --host-level laptop
```

The command prints the configuration file, the level of this machine and its destination. When the destination exists, it also prints the free space and whether the destination is on the same file system as the experiments.
