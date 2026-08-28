<?php
// Replaces existing www-data-owned legacy source files with symlinks to unique OneDrive originals.
// Requires: run as root or www-data so existing source paths can be removed.
$rows = array_map(static function ($line) {
    return str_getcsv($line, "\t");
}, file('/tmp/legacy-2011-2014-upgrade.tsv', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES));
array_shift($rows);
$done=$skip=$fail=0; $errors=[];
foreach($rows as $r){
 [$id,$album,$filename,$target,$orig,$targetSize,$odSize]=$r;
 if(!file_exists($orig)){$skip++;continue;}
 if(is_link($target) && readlink($target)===$orig){$skip++;continue;}
 if(!file_exists($target) && !is_link($target)){$errors[]="missing target $target";$fail++;continue;}
 if(!unlink($target)){$errors[]="unlink failed $target";$fail++;continue;}
 if(!symlink($orig,$target)){$errors[]="symlink failed $target";$fail++;continue;}
 // Keep ownership consistent with the rest of wppa-source; symlink modes are not portable.
 if(function_exists('lchown') && !lchown($target,'www-data')){$errors[]="lchown failed $target";}
 if(function_exists('lchgrp') && !lchgrp($target,'www-data')){$errors[]="lchgrp failed $target";}
 $done++;
}
echo "done=$done skipped=$skip failed=$fail\n";
foreach(array_slice($errors,0,30) as $e) echo "$e\n";
